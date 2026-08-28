"""On-disk :class:`~webglass.sessions.SessionStore` (build plan task t12).

:class:`webglass.sessions.InMemorySessionStore` cannot answer the M2 promise
that "state set by an action in one CLI invocation is visible to a later read
in the same session across separate subprocesses" (spec claim c29): a
one-shot CLI process takes its dict to the grave. This module is the store
that survives the process — it holds the CDP endpoint of a *detached* browser
(see :mod:`webglass.adapters.playwright`) so a later, unrelated invocation can
reattach to the same live page.

It implements the :class:`~webglass.sessions.SessionStore` protocol and
nothing more: the four state kinds stay separate (CLAUDE.md "Target
architecture" section 2), so nothing here writes evidence, exploration edges,
or memory entries.

Where it lives, and what it is not
----------------------------------

Session records are **runtime state**, not durable product data: a record is
worthless the moment its browser process is gone. So they live in a per-user
*state* directory, resolved in this order:

1. ``$WEBGLASS_STATE_DIR`` — the escape hatch, used by the test suite;
2. ``$XDG_STATE_HOME/webglass``;
3. ``~/.local/state/webglass``.

Sessions occupy ``<state root>/sessions``::

    sessions/                       0700  the directory itself
      <session-id>.json             0600  one record, the endpoint inside it
      <session-id>.lock             0600  flock target for that record
      profiles/<session-id>/        0700  the browser's --user-data-dir

This placement is **not** the M3 decision about where the SQLite metadata
store and content-addressed artifact store live — that question (per-user XDG
data dir vs per-workspace) is explicitly parked in the implementation spec's
open parks and stays parked. Evidence and web-memory are durable; a browser
session is not, and the two must not be conflated because one of them
happened to need a path first.

The endpoint is secret-equivalent
---------------------------------

Whoever holds the CDP endpoint owns the browser (spec claim c33 / honesty
h30). Three defenses, all tested in ``tests/test_session_persistence.py``:

* **On disk** — the record file is created ``0600`` through ``os.open`` with
  an explicit mode (never ``open()`` + a later ``chmod`` race), inside a
  ``0700`` directory. Atomic replacement goes through
  :func:`tempfile.mkstemp`, which is ``0600`` from birth, so no window exists
  in which the endpoint is world-readable.
* **In memory** — :class:`FileSessionRecord` inherits
  :class:`~webglass.sessions.SessionRecord`'s redacted ``repr()`` and its
  ``to_public_dict()``, which omits ``endpoint_ref`` entirely. The extra
  fields this subclass publishes (pid, profile directory, sandbox posture,
  reap outcome) are deliberately *not* secrets, and the endpoint is never
  added back.
* **On the way out** — :meth:`FileSessionStore.endpoint_for` is the single
  method that returns it, and it exists for exactly one caller: the endpoint
  resolver seam of
  :class:`webglass.adapters.playwright.PlaywrightBrowserBackend`. Nothing
  else in WebGlass should call it, and nothing in this module logs it.

Locking: ``flock``, plus expiry for the holder that died
--------------------------------------------------------

Two different failure modes need two different mechanisms, and conflating
them is how session stores deadlock forever:

* **Torn read-modify-write** (two processes updating one record at once) is
  prevented by an advisory ``fcntl.flock`` on a per-session ``.lock`` file,
  held only for the microseconds of a read/modify/write. ``flock`` is chosen
  over an ``O_EXCL`` lock file precisely because the kernel releases it when
  the holder dies: a crashed process leaves no lock to break. (Advisory
  locking over NFS is unreliable — a shared network state directory is out of
  scope, and the state root is a per-user local path by default.)
* **A crashed lease holder** is handled by the lease's own ``expires_at``,
  exactly as :class:`~webglass.sessions.InMemorySessionStore` does. A holder
  that never released is simply overtaken once its lease expires.

Because ``flock`` is per open-file-description, a *second* lock acquisition
on the same session inside one process would block on itself. Every method
here takes the lock at most once and calls ``_unlocked`` helpers underneath.

Cleanup is a process story, not just a record story
---------------------------------------------------

A session whose record was deleted while its browser kept running is an
orphan nobody can reach and nobody will kill. :meth:`FileSessionStore.clean`
therefore reaps in the honest order: mark the record expired, terminate the
browser by its stored pid (``SIGTERM``, then ``SIGKILL``; an already-dead pid
is not an error), delete the profile directory, and report what happened on
the returned records — which the CLI renders straight into ``session clean``'s
JSON. :meth:`FileSessionStore.close` does the same for a deliberate close.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from webglass.sessions import (
    DEFAULT_LEASE_TTL_SECONDS,
    Lease,
    LeaseGrant,
    LeaseRefusal,
    SessionRecord,
    SessionStatus,
)

__all__ = [
    "ALLOW_UNSANDBOXED_ENV",
    "DEFAULT_RECORD_RETENTION_SECONDS",
    "PROFILES_DIRNAME",
    "RECORD_SCHEMA_VERSION",
    "SESSIONS_DIRNAME",
    "STATE_DIR_ENV",
    "CorruptSessionRecord",
    "FileSessionRecord",
    "FileSessionStore",
    "LaunchedBrowser",
    "SessionLaunchError",
    "SessionRecordError",
    "default_sessions_dir",
    "default_state_root",
    "make_playwright_launcher",
    "terminate_pid",
]

#: Record-format version, independent of the operation/result schema versions
#: (``webglass.operations.SCHEMA_VERSION`` / ``webglass.results.SCHEMA_VERSION``):
#: this is a private on-disk layout, not a caller-visible contract. A record
#: written by a different version is refused rather than guessed at.
RECORD_SCHEMA_VERSION = 1

#: Overrides the whole state root (the test suite points this at a tmp dir).
STATE_DIR_ENV = "WEBGLASS_STATE_DIR"

#: The deployment-posture opt-in :func:`make_playwright_launcher` reads when it
#: is not told explicitly. Mirrors the adapter's ``allow_unsandboxed``: only a
#: human configuring a host whose kernel/AppArmor blocks the Chromium sandbox
#: sets it, and the choice is recorded on every session it creates.
ALLOW_UNSANDBOXED_ENV = "WEBGLASS_ALLOW_UNSANDBOXED"

SESSIONS_DIRNAME = "sessions"
PROFILES_DIRNAME = "profiles"
RECORD_SUFFIX = ".json"
LOCK_SUFFIX = ".lock"

_DIR_MODE = 0o700
_FILE_MODE = 0o600

#: How long a non-active record file is kept before :meth:`FileSessionStore.clean`
#: purges it. Closed/expired records stay readable by ``session show`` for a
#: while (an agent asking "what happened to my session?" deserves an answer),
#: but not forever — an unbounded directory of dead records is a disk leak.
DEFAULT_RECORD_RETENTION_SECONDS = 7 * 24 * 60 * 60.0

#: Session ids become filenames, so they are validated rather than trusted:
#: ``../`` or an absolute path in a caller-supplied ``--session-id`` must never
#: be able to name a file outside the sessions directory.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

_TERMINATE_GRACE_SECONDS = 5.0
_TERMINATE_POLL_SECONDS = 0.05


class SessionRecordError(RuntimeError):
    """A record file exists but cannot be read as a session record.

    Deliberately loud. Silently treating a corrupt record as "no such
    session" would hand a caller a fresh session while an unreachable browser
    kept running — the orphan case this store exists to prevent.
    :meth:`FileSessionStore.clean` is the recovery path: it removes
    unreadable records.
    """


class SessionLaunchError(RuntimeError):
    """A session's browser could not be started.

    Carries the same ``code``/``message``/``remediation`` triple as
    :class:`webglass.results.OperationError` and
    :class:`webglass.adapters.playwright.BrowserLaunchError`. ``str()``
    includes the remediation because the operation service's exception
    boundary renders an unexpected exception through its message alone — an
    unactionable "session create failed" would be a worse answer than a long
    one.
    """

    def __init__(self, code: str, message: str, remediation: str = "") -> None:
        super().__init__(f"{message} — {remediation}" if remediation else message)
        self.code = code
        self.message = message
        self.remediation = remediation

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "remediation": self.remediation}


@dataclass(frozen=True)
class LaunchedBrowser:
    """What a :data:`SessionLauncher` hands back: a running, detached browser.

    A deliberately small mirror of
    :class:`webglass.adapters.playwright.DetachedBrowser` — this module must
    describe a launched browser without depending on Playwright at import
    time, so a fake launcher (every non-browser test) constructs one of these
    with no browser anywhere in sight. :attr:`endpoint` is secret-equivalent
    and redacted from ``repr()`` on the same terms.
    """

    endpoint: str
    pid: int
    user_data_dir: str
    sandboxed: bool = True
    diagnostics: tuple[str, ...] = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"LaunchedBrowser(endpoint=<redacted>, pid={self.pid!r}, "
            f"user_data_dir={self.user_data_dir!r}, sandboxed={self.sandboxed!r}, "
            f"diagnostics={self.diagnostics!r})"
        )


#: Starts one session's browser. Takes the session id and the profile
#: directory this store has reserved for it; returns a :class:`LaunchedBrowser`
#: or raises :class:`SessionLaunchError`. The seam exists so the store can be
#: driven with a fake launcher in tests and with real Chromium in production.
SessionLauncher = Callable[[str, Path], LaunchedBrowser]

#: Stops a process by pid; returns whether a live process was actually
#: signalled. Injectable for the same reason.
ProcessTerminator = Callable[[int], bool]


def default_state_root(environ: Mapping[str, str] | None = None) -> Path:
    """The per-user WebGlass state root (see the module docstring's order)."""
    env = os.environ if environ is None else environ
    override = env.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    xdg = env.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "webglass"


def default_sessions_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The directory :class:`FileSessionStore` uses when given none."""
    return default_state_root(environ) / SESSIONS_DIRNAME


#: The three outcomes of probing a pid with ``kill(pid, 0)``. ``"foreign"`` is
#: split out from ``"running"`` because the two answer different questions:
#: *is something alive at this pid* (yes, for both) versus *is that something
#: our browser* (yes only for ``"running"``) — see :func:`_pid_liveness`.
_PidLiveness = str  # "running" | "dead" | "foreign"


def _pid_liveness(pid: int) -> _PidLiveness:
    """Probe ``pid`` with a signal-0 ``kill`` and classify what answered.

    ``"dead"`` — ``ProcessLookupError``: nothing with this pid exists.

    ``"foreign"`` — ``PermissionError``: something with this pid exists, but
    the OS refuses to let us signal it because it belongs to another user.
    On a long-lived host this is overwhelmingly pid reuse *after* our
    browser already exited, not our own browser somehow surviving under a
    different uid — the pid space wrapped around and the OS handed our old
    number to someone else's process. Callers must never fold this into
    "our browser is alive" (spec honesty h4); see ``to_public_dict``'s
    ``observed_liveness`` field, which reports it as ``"unknown"`` rather
    than claiming the process as ours.

    ``"running"`` — the signal was accepted: a process we are allowed to
    signal exists at this pid.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "foreign"
    return "running"


def _is_running(pid: int) -> bool:
    """Whether *something* answers at ``pid`` — collapses ``"foreign"`` into
    "yes" because this predicate is used by :func:`terminate_pid` to decide
    whether to keep waiting/escalate, not to decide ownership. Anything
    needing the ownership distinction uses :func:`_pid_liveness` directly."""
    return _pid_liveness(pid) != "dead"


def _reap_child(pid: int) -> None:
    """Collect ``pid`` if it happens to be *this* process's child.

    A browser launched by this very process becomes a zombie the moment it
    dies until someone waits on it, and a zombie still answers ``kill(pid, 0)``
    — so without this, terminating a browser launched in-process would look
    like a five-second refusal to die. A browser launched by an earlier CLI
    invocation is not our child at all (``ChildProcessError``), which is the
    ordinary case and equally fine.
    """
    with suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


def terminate_pid(
    pid: int,
    *,
    grace_seconds: float = _TERMINATE_GRACE_SECONDS,
    poll_seconds: float = _TERMINATE_POLL_SECONDS,
) -> bool:
    """Stop ``pid``, escalating ``SIGTERM`` → ``SIGKILL``. Idempotent.

    Returns ``True`` if a live process was signalled and ``False`` if the pid
    was already gone (or never ours to signal) — the distinction ``session
    clean`` reports, so "the browser was already dead" reads differently from
    "we killed it". Never raises for a dead pid: reaping a crashed caller's
    leftovers is the normal path, not an error.

    A ``PermissionError`` means the pid now belongs to another user — almost
    certainly pid reuse after our browser exited. Signalling harder would be
    the one genuinely dangerous thing this function could do, so it stops.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _reap_child(pid)
        return False
    except PermissionError:  # pragma: no cover - needs a foreign pid to exercise
        return False

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        _reap_child(pid)
        if not _is_running(pid):
            return True
        time.sleep(poll_seconds)
    with suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)
    _reap_child(pid)
    return True


def make_playwright_launcher(*, allow_unsandboxed: bool | None = None) -> SessionLauncher:
    """A :data:`SessionLauncher` backed by real detached Chromium.

    ``allow_unsandboxed=None`` (the default) reads
    :data:`ALLOW_UNSANDBOXED_ENV` at *launch* time, so the posture is a
    deployment decision made by whoever runs the process — never a fallback
    this code takes on its own. When it is on, the sandbox-disabled marker
    from the adapter is carried onto the session record, where ``session
    create``/``session show`` render it in plain sight.

    Playwright is imported lazily, inside the launch: building a store must
    not cost a browser-driver import, and the non-browser posture must not
    require Playwright to be importable at all.
    """

    def launch(session_id: str, user_data_dir: Path) -> LaunchedBrowser:
        from webglass.adapters.playwright import BrowserLaunchError, launch_detached

        unsandboxed = (
            os.environ.get(ALLOW_UNSANDBOXED_ENV) == "1"
            if allow_unsandboxed is None
            else allow_unsandboxed
        )
        try:
            browser = launch_detached(user_data_dir, allow_unsandboxed=unsandboxed)
        except BrowserLaunchError as exc:
            raise SessionLaunchError(exc.code, exc.message, exc.remediation) from exc
        return LaunchedBrowser(
            endpoint=browser.endpoint,
            pid=browser.pid,
            user_data_dir=browser.user_data_dir,
            sandboxed=browser.sandboxed,
            diagnostics=tuple(browser.diagnostics),
        )

    return launch


@dataclass(repr=False)
class FileSessionRecord(SessionRecord):
    """A :class:`~webglass.sessions.SessionRecord` plus its process facts.

    Extends rather than replaces, so every existing consumer — the service's
    ``to_public_dict()`` rendering, the redacted ``repr()``, the lease fields —
    keeps working untouched. The added fields are the ones a caller needs to
    reason about a *live* session and none of them is a secret:

    ``pid`` / ``user_data_dir``
        Which process and profile directory this session owns, so ``session
        clean`` can prove it reaped them.
    ``sandboxed`` / ``diagnostics``
        The sandbox posture the browser was launched under. A session running
        without the Chromium sandbox says so on every render of its record —
        the "loud diagnostic" half of the explicit opt-in.
    ``browser_reaped`` / ``browser_was_running``
        The outcome of a close/clean: whether this store terminated the
        browser, and whether it found it alive to terminate. ``None`` means
        no attempt was made.

    ``repr=False`` is load-bearing, not style: ``@dataclass`` generates a
    ``__repr__`` by default, and a generated one would print *every* field —
    silently replacing the base class's redacting ``repr()`` and putting the
    endpoint back into every debugger session, log line, and assertion
    message. Suppressing generation inherits the redaction instead.
    """

    pid: int | None = None
    user_data_dir: str = ""
    sandboxed: bool = True
    diagnostics: tuple[str, ...] = ()
    browser_reaped: bool = False
    browser_was_running: bool | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """The base record's safe dict, plus the process facts — never the endpoint.

        ``observed_liveness`` is computed fresh on every call from a live
        ``kill(pid, 0)`` probe (spec claim c7 / honesty h4): it is an
        *observed fact at render time*, not a stored field and not something
        this method ever writes back to the record or the file on disk — a
        record marked ``active`` with a long-dead pid renders as ``"dead"``
        on this and every future call, forever, until something actually
        reaps it (``session clean``/``close``, a separate write path). Values:

        * ``"running"`` — the pid answered and we are allowed to signal it;
        * ``"dead"`` — nothing answers at that pid any more;
        * ``"unknown"`` — no pid was ever recorded, *or* a pid answered but
          belongs to another user (see :func:`_pid_liveness`) — that case is
          deliberately not reported as ``"running"``: it is almost always
          pid reuse after our browser already exited, and claiming it as
          "ours, alive" would be exactly the false confidence this field
          exists to prevent.
        """
        payload = super().to_public_dict()
        payload.update(
            {
                "pid": self.pid,
                "user_data_dir": self.user_data_dir,
                "sandboxed": self.sandboxed,
                "diagnostics": list(self.diagnostics),
                "browser_reaped": self.browser_reaped,
                "browser_was_running": self.browser_was_running,
                "observed_liveness": self._observed_liveness(),
            }
        )
        return payload

    def _observed_liveness(self) -> str:
        """See ``observed_liveness`` in :meth:`to_public_dict`'s docstring."""
        if self.pid is None:
            return "unknown"
        liveness = _pid_liveness(self.pid)
        if liveness == "foreign":
            return "unknown"
        return liveness


@dataclass(frozen=True)
class CorruptSessionRecord:
    """A session record file that exists but cannot be parsed, as reportable data.

    :meth:`FileSessionStore.list` used to raise :class:`SessionRecordError`
    the moment it hit one bad file (build plan task t12's doctor
    session-store check would have inherited that: a health check must not
    be taken down by the very condition it exists to report — issue #14
    task t5). Reads now tolerate a corrupt record the same way
    :meth:`FileSessionStore.clean` already does, but a read path must not
    silently swallow the corruption either — this is the data a caller
    (``session list``, and later the doctor check) renders instead of the
    record it could not load. Deliberately carries no file contents: the
    bytes that failed to parse might not even be JSON, so there is nothing
    safe to echo back beyond the id, path, and parser's own message.
    """

    session_id: str
    path: str
    error: str


class FileSessionStore:
    """A :class:`~webglass.sessions.SessionStore` whose state outlives the process.

    Constructed per CLI invocation (it is cheap: all state is on disk and
    nothing is cached in the instance, so two instances — or two processes —
    see exactly the same sessions).

    ``launcher`` is optional and its absence is a real posture, not a
    degraded one: with no launcher, ``session create`` records a session
    without starting a browser, which is what every non-browser caller and the
    entire default test suite want. Wiring the Playwright launcher in is the
    CLI factory's decision (see :mod:`webglass.cli._factory`).
    """

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        launcher: SessionLauncher | None = None,
        terminator: ProcessTerminator = terminate_pid,
        record_retention_seconds: float = DEFAULT_RECORD_RETENTION_SECONDS,
    ) -> None:
        #: The sessions directory. Nothing is created until a write happens —
        #: constructing a store must have no filesystem side effects, so a
        #: caller that never touches a session leaves no directory behind.
        self.directory = Path(directory) if directory is not None else default_sessions_dir()
        self.launcher = launcher
        self.terminator = terminator
        self.record_retention_seconds = record_retention_seconds

    # -- SessionStore -------------------------------------------------------

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
    ) -> FileSessionRecord:
        """Create, persist, and (if a launcher is wired) launch a session.

        An explicit ``endpoint_ref`` is adopted as-is and suppresses the
        launch — that is how a caller attaches a session record to a browser
        it started itself, and how the endpoint-secrecy tests plant a known
        secret without needing Chromium.
        """
        _validate_session_id(session_id)
        with self._locked(session_id):
            if self._record_path(session_id).exists():
                raise ValueError(f"session_id already exists: {session_id}")

            profile_ref, notes = _jsonable(capability_profile_ref)
            record = FileSessionRecord(
                session_id=session_id,
                generation=0,
                owner=owner,
                caller=caller,
                task=task,
                backend_id=backend_id,
                created_at=now,
                last_used_at=now,
                expires_at=expires_at,
                capability_profile_ref=profile_ref,
                status=SessionStatus.ACTIVE,
                lease=None,
                endpoint_ref=endpoint_ref,
                diagnostics=notes,
            )
            if not endpoint_ref and self.launcher is not None:
                self._launch(record)
            self._write_unlocked(record)
            return record

    def get(self, session_id: str) -> FileSessionRecord | None:
        """Return the stored record, or ``None`` if there is none.

        Read without taking the lock: writes land through an atomic
        ``os.replace``, so a reader sees either the whole previous record or
        the whole new one, never a torn file.
        """
        if not _valid_session_id(session_id):
            return None
        return self._read_unlocked(session_id)

    def list(self) -> list[FileSessionRecord]:
        """Every readable record, oldest first.

        Tolerates a corrupt record file the same way :meth:`clean` does —
        one unparseable record must not take down a read of every other
        session (issue #14 task t5; a doctor health check is built on this
        exact path). The record is skipped here, never purged (only
        :meth:`clean` mutates); call :meth:`list_corrupt` to see what was
        skipped and why.
        """
        records, _corrupt = self._list_all()
        return records

    def list_corrupt(self) -> list[CorruptSessionRecord]:
        """Record files :meth:`list` had to skip because they would not parse.

        A read path must not silently swallow corruption — this is how a
        caller (``session list``, and the doctor session-store check built
        on top of this store) learns *which* record is broken without
        :meth:`list` itself raising.
        """
        _records, corrupt = self._list_all()
        return corrupt

    def _list_all(self) -> tuple[list[FileSessionRecord], list[CorruptSessionRecord]]:
        records: list[FileSessionRecord] = []
        corrupt: list[CorruptSessionRecord] = []
        for session_id in self._session_ids():
            try:
                record = self._read_unlocked(session_id)
            except SessionRecordError as exc:
                corrupt.append(
                    CorruptSessionRecord(
                        session_id=session_id,
                        path=str(self._record_path(session_id)),
                        error=str(exc),
                    )
                )
                continue
            if record is not None:
                records.append(record)
        records.sort(key=lambda record: (record.created_at, record.session_id))
        corrupt.sort(key=lambda item: item.session_id)
        return records, corrupt

    def close(self, session_id: str) -> None:
        """Close a session and stop its browser.

        A closed session is the deliberate end of a live browser, so the
        process is terminated and its profile directory removed here rather
        than left for ``clean``. The record itself stays (as ``closed``) so
        ``session show`` can still answer for it.
        """
        with self._locked(session_id):
            record = self._require_unlocked(session_id)
            if record.status is SessionStatus.ACTIVE:
                self._reap_browser(record)
            record.status = SessionStatus.CLOSED
            record.lease = None
            self._write_unlocked(record)

    def clean(self, now: float) -> list[FileSessionRecord]:
        """Reap expired sessions, their browsers, and their leftovers.

        Four separate jobs, in one deterministic pass:

        1. an **active session past its ``expires_at``** becomes ``expired``,
           its browser is terminated, and its profile directory is removed —
           this is what keeps a crashed caller from leaving an orphan browser
           alive forever;
        2. an **expired lease on a still-live session** is dropped, so the
           next caller sees a free session rather than inferring liveness from
           a timestamp;
        3. a **long-dead record** (closed/expired past the retention window)
           is purged, bounding the directory;
        4. an **unreadable record file** is removed, because this is the only
           recovery path for one.

        Only case 1 is returned — those are the sessions a caller would call
        "reaped", and the service renders exactly them.
        """
        reaped: list[FileSessionRecord] = []
        for session_id in self._session_ids():
            with self._locked(session_id):
                try:
                    record = self._read_unlocked(session_id)
                except SessionRecordError:
                    self._purge_unlocked(session_id)
                    continue
                if record is None:
                    continue
                if record.status is SessionStatus.ACTIVE and record.expires_at <= now:
                    record.status = SessionStatus.EXPIRED
                    record.lease = None
                    self._reap_browser(record)
                    self._write_unlocked(record)
                    reaped.append(record)
                elif (
                    record.status is SessionStatus.ACTIVE
                    and record.lease is not None
                    and record.lease.expires_at <= now
                ):
                    record.lease = None
                    self._write_unlocked(record)
                elif record.status is not SessionStatus.ACTIVE and self._purgeable(record, now):
                    self._purge_unlocked(session_id)
        return reaped

    def acquire_lease(
        self,
        session_id: str,
        holder: str,
        now: float,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> LeaseGrant | LeaseRefusal:
        """Take exclusive control of a session, or report who has it.

        Same semantics as :class:`~webglass.sessions.InMemorySessionStore`
        (renewal by the same holder succeeds; an expired lease is up for
        grabs), enforced across *processes* rather than across threads. The
        refusal is an ordinary return value: two CLI invocations racing for
        one browser is normal traffic, not an error.
        """
        with self._locked(session_id):
            record = self._require_unlocked(session_id)
            if record.status is not SessionStatus.ACTIVE:
                return LeaseRefusal(
                    session_id=session_id,
                    requested_by=holder,
                    held_by="",
                    reason=f"session_not_active:{record.status.value}",
                )
            current = record.lease
            if current is not None and current.holder != holder and current.expires_at > now:
                return LeaseRefusal(
                    session_id=session_id,
                    requested_by=holder,
                    held_by=current.holder,
                    reason="lease_held",
                )
            record.lease = Lease(holder=holder, acquired_at=now, expires_at=now + ttl_seconds)
            record.last_used_at = now
            self._write_unlocked(record)
            return LeaseGrant(
                session_id=session_id,
                holder=holder,
                acquired_at=now,
                expires_at=record.lease.expires_at,
            )

    def release_lease(self, session_id: str, holder: str) -> bool:
        with self._locked(session_id):
            record = self._require_unlocked(session_id)
            if record.lease is not None and record.lease.holder == holder:
                record.lease = None
                self._write_unlocked(record)
                return True
            return False

    def bump_generation(self, session_id: str) -> FileSessionRecord:
        with self._locked(session_id):
            record = self._require_unlocked(session_id)
            record.generation += 1
            self._write_unlocked(record)
            return record

    # -- the endpoint seam --------------------------------------------------

    def endpoint_for(self, session_id: str) -> str:
        """Resolve a session id to its connect endpoint. **Returns a secret.**

        This is the callable
        :class:`webglass.adapters.playwright.PlaywrightBrowserBackend` accepts
        as its endpoint resolver, and the only sanctioned way the endpoint
        leaves this store. It is never logged, never rendered, and never part
        of any ``to_dict``/``to_public_dict``.

        Raises :class:`LookupError` — with an actionable message that names no
        endpoint — when the session is unknown, not active, or has no browser.
        """
        record = self.get(session_id)
        if record is None:
            raise LookupError(
                f"no session {session_id!r} in this store; run 'webglass session create' "
                "first, or pass an existing --session-id"
            )
        if record.status is not SessionStatus.ACTIVE:
            raise LookupError(
                f"session {session_id!r} is {record.status.value}; create a new session"
            )
        if not record.endpoint_ref:
            raise LookupError(
                f"session {session_id!r} has no browser attached (it was created without a "
                "browser launcher); create a session with the browser backend enabled"
            )
        return record.endpoint_ref

    # -- paths and ids ------------------------------------------------------

    def _record_path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}{RECORD_SUFFIX}"

    def _lock_path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}{LOCK_SUFFIX}"

    def _profile_path(self, session_id: str) -> Path:
        return self.directory / PROFILES_DIRNAME / session_id

    def _session_ids(self) -> list[str]:
        try:
            entries = sorted(self.directory.iterdir())
        except FileNotFoundError:
            return []
        return [entry.name[: -len(RECORD_SUFFIX)] for entry in entries if _is_record(entry)]

    def _ensure_directory(self) -> None:
        self.directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        # mkdir's mode is masked by the process umask, and the directory may
        # predate a stricter policy — so state the permission rather than hope.
        os.chmod(self.directory, _DIR_MODE)

    # -- locking ------------------------------------------------------------

    @contextmanager
    def _locked(self, session_id: str) -> Iterator[None]:
        """Hold this session's ``flock`` for one read-modify-write."""
        _validate_session_id(session_id)
        self._ensure_directory()
        lock_path = self._lock_path(session_id)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, _FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- record I/O ---------------------------------------------------------

    def _read_unlocked(self, session_id: str) -> FileSessionRecord | None:
        path = self._record_path(session_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SessionRecordError(f"session record {path} is unreadable: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SessionRecordError(
                f"session record {path} is not valid JSON: {exc}; run 'webglass session clean' "
                "to remove unreadable records"
            ) from exc
        return _from_payload(payload, path)

    def _require_unlocked(self, session_id: str) -> FileSessionRecord:
        record = self._read_unlocked(session_id)
        if record is None:
            raise KeyError(f"unknown session_id: {session_id}")
        return record

    def _write_unlocked(self, record: FileSessionRecord) -> None:
        """Persist a record atomically, at ``0600``, never world-readable.

        ``mkstemp`` creates the temporary file ``0600`` from birth in the same
        directory, so ``os.replace`` (atomic within a filesystem) publishes a
        file that was never readable by anyone else — the endpoint inside it
        has no exposure window.
        """
        self._ensure_directory()
        data = json.dumps(_to_payload(record), indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(
            dir=str(self.directory), prefix=f".{record.session_id}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
            os.chmod(temp_name, _FILE_MODE)
            os.replace(temp_name, self._record_path(record.session_id))
        except BaseException:
            with suppress(OSError):
                os.unlink(temp_name)
            raise

    def _purge_unlocked(self, session_id: str) -> None:
        self._record_path(session_id).unlink(missing_ok=True)
        self._lock_path(session_id).unlink(missing_ok=True)
        shutil.rmtree(self._profile_path(session_id), ignore_errors=True)

    def _purgeable(self, record: FileSessionRecord, now: float) -> bool:
        age = now - max(record.last_used_at, record.expires_at)
        return age >= self.record_retention_seconds

    # -- browser lifecycle --------------------------------------------------

    def _launch(self, record: FileSessionRecord) -> None:
        """Start this session's browser and record where it lives.

        A failed launch leaves nothing behind: the profile directory is
        removed and the error propagates, so ``session create`` never returns
        a record pointing at a browser that does not exist.
        """
        assert self.launcher is not None  # nosec B101 - guarded by the caller
        profile = self._profile_path(record.session_id)
        profile.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        os.chmod(profile, _DIR_MODE)
        try:
            launched = self.launcher(record.session_id, profile)
        except BaseException:
            shutil.rmtree(profile, ignore_errors=True)
            raise
        record.endpoint_ref = launched.endpoint
        record.pid = launched.pid
        record.user_data_dir = launched.user_data_dir or str(profile)
        record.sandboxed = launched.sandboxed
        record.diagnostics = record.diagnostics + tuple(launched.diagnostics)

    def _reap_browser(self, record: FileSessionRecord) -> None:
        """Stop this session's browser and drop everything that names it."""
        if record.pid is not None:
            record.browser_was_running = bool(self.terminator(record.pid))
            record.browser_reaped = True
        # The endpoint names a browser that is gone: keeping it would be a
        # secret with no purpose, and a resolver that hands out a dead
        # endpoint is worse than one that admits there is none.
        record.endpoint_ref = ""
        if record.user_data_dir:
            shutil.rmtree(record.user_data_dir, ignore_errors=True)
        shutil.rmtree(self._profile_path(record.session_id), ignore_errors=True)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _to_payload(record: FileSessionRecord) -> dict[str, Any]:
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "session_id": record.session_id,
        "generation": record.generation,
        "owner": record.owner,
        "caller": record.caller,
        "task": record.task,
        "backend_id": record.backend_id,
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
        "expires_at": record.expires_at,
        "capability_profile_ref": record.capability_profile_ref,
        "status": record.status.value,
        "lease": (
            None
            if record.lease is None
            else {
                "holder": record.lease.holder,
                "acquired_at": record.lease.acquired_at,
                "expires_at": record.lease.expires_at,
            }
        ),
        # Secret-equivalent, and the reason this file is 0600 in a 0700 dir.
        "endpoint_ref": record.endpoint_ref,
        "pid": record.pid,
        "user_data_dir": record.user_data_dir,
        "sandboxed": record.sandboxed,
        "diagnostics": list(record.diagnostics),
        "browser_reaped": record.browser_reaped,
        "browser_was_running": record.browser_was_running,
    }


def _from_payload(payload: Any, path: Path) -> FileSessionRecord:
    if not isinstance(payload, dict):
        raise SessionRecordError(f"session record {path} is not a JSON object")
    version = payload.get("schema_version")
    if version != RECORD_SCHEMA_VERSION:
        raise SessionRecordError(
            f"session record {path} has schema_version {version!r}, expected "
            f"{RECORD_SCHEMA_VERSION}; run 'webglass session clean' to remove records "
            "written by an incompatible version"
        )
    lease_data = payload.get("lease")
    try:
        return FileSessionRecord(
            session_id=str(payload["session_id"]),
            generation=int(payload["generation"]),
            owner=str(payload["owner"]),
            caller=str(payload["caller"]),
            task=str(payload["task"]),
            backend_id=str(payload["backend_id"]),
            created_at=float(payload["created_at"]),
            last_used_at=float(payload["last_used_at"]),
            expires_at=float(payload["expires_at"]),
            capability_profile_ref=payload.get("capability_profile_ref"),
            status=SessionStatus(str(payload["status"])),
            lease=(
                None
                if lease_data is None
                else Lease(
                    holder=str(lease_data["holder"]),
                    acquired_at=float(lease_data["acquired_at"]),
                    expires_at=float(lease_data["expires_at"]),
                )
            ),
            endpoint_ref=str(payload.get("endpoint_ref", "")),
            pid=None if payload.get("pid") is None else int(payload["pid"]),
            user_data_dir=str(payload.get("user_data_dir", "")),
            sandboxed=bool(payload.get("sandboxed", True)),
            diagnostics=tuple(str(item) for item in payload.get("diagnostics", ())),
            browser_reaped=bool(payload.get("browser_reaped", False)),
            browser_was_running=(
                None
                if payload.get("browser_was_running") is None
                else bool(payload["browser_was_running"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionRecordError(f"session record {path} is malformed: {exc}") from exc


def _jsonable(value: Any) -> tuple[Any, tuple[str, ...]]:
    """Make ``capability_profile_ref`` storable, loudly if it must be changed.

    The field is typed ``Any`` by the protocol, but this store has to write
    JSON. Rather than drop or silently mangle an object a library caller
    passed, it is stringified *and* the substitution is recorded as a
    diagnostic on the record — where ``session show`` renders it.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return (
            repr(value),
            (
                "capability-profile-ref-stringified: the value was not JSON-serializable "
                f"and was stored as repr() of a {type(value).__name__}",
            ),
        )
    return value, ()


# ---------------------------------------------------------------------------
# Session id validation
# ---------------------------------------------------------------------------


def _valid_session_id(session_id: str) -> bool:
    return isinstance(session_id, str) and _SESSION_ID_RE.fullmatch(session_id) is not None


def _validate_session_id(session_id: str) -> None:
    if not _valid_session_id(session_id):
        raise ValueError(
            f"invalid session id {session_id!r}: a session id becomes a filename, so it must "
            "be 1-128 characters of letters, digits, '.', '_' or '-', starting with a letter "
            "or digit"
        )


def _is_record(path: Path) -> bool:
    return (
        path.name.endswith(RECORD_SUFFIX)
        and not path.name.startswith(".")
        and _valid_session_id(path.name[: -len(RECORD_SUFFIX)])
    )
