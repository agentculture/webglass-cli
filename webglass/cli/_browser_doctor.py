"""Browser-capability diagnostics for ``webglass doctor`` (build plan task t14).

Extends the doctor's rubric-shaped check contract
(``{id, passed, severity, message, remediation}``, see ``_commands/doctor.py``)
with five checks that answer "can this host actually run the Playwright
adapter" (spec claim c32 / honesty h29):

- ``playwright_importable`` — the ``playwright`` package imports cleanly.
  Playwright is a *declared core runtime dependency*
  (``pyproject.toml``'s ``dependencies``, not an extra), so this failing is a
  broken invariant, not an absent optional feature: ``severity="error"``.
- ``chromium_installed`` — :func:`webglass.adapters.playwright.chromium_executable_path`
  resolves to a file that exists on disk.
- ``playwright_version`` — the installed Playwright version plus the pinned
  range declared in ``pyproject.toml`` (read back via package metadata so it
  is accurate for both a source checkout and a wheel install).
- ``usable_sandbox`` — whether a *sandboxed* Chromium launch is plausible on
  this host, probed the same way
  :data:`webglass.adapters.playwright.SANDBOX_REMEDIATION` describes:
  ``/proc/sys/kernel/apparmor_restrict_unprivileged_userns`` (``1`` means
  AppArmor restricts unprivileged user namespaces — the Ubuntu
  23.10+/24.04-class condition this host was developed against, see risk r4
  in ``docs/plans/2026-08-07-implement-webglass-issue-1.md``) and
  ``kernel.unprivileged_userns_clone`` (``0`` means disabled outright). This
  check only ever *reads* those files — it never touches ``--no-sandbox``;
  that stays an explicit, loud, caller-only opt-in inside the adapter itself.
- ``state_dir_writable`` — the WebGlass state directory
  (``$XDG_STATE_HOME/webglass``, falling back to ``~/.local/state/webglass``)
  is creatable and writable. This is a doctor-only environment probe, not a
  commitment to the final on-disk layout of the SQLite/artifact stores — that
  placement decision is still an open park (see the M0-M2 spec's "Open
  parks" section); it may move once M3 decides that layout.
- ``session_store_health`` (build plan task t12, issue #14) — a read-only
  survey of the session-record store: how many records are ``active`` with
  a live pid ("live") versus ``active`` with a dead/foreign/absent pid
  ("stale" — exactly the "records marked active with dead pids read as live
  forever" defect issue #14 reported), the total record count, the on-disk
  size of the record directory, and how many record files were too corrupt
  to parse. This is the check whose absence is why issue #14's reporter had
  to count 126 stray records by hand. It is built entirely on
  :meth:`~webglass.adapters.session_store.FileSessionStore.list` and
  :meth:`~webglass.adapters.session_store.FileSessionStore.list_corrupt`
  (issue #14 task t5's corruption-tolerant reads — a health check must not
  be taken down by the very condition it exists to report) and on
  :meth:`~webglass.adapters.session_store.FileSessionRecord.to_public_dict`'s
  ``observed_liveness`` (task t3) to tell live from stale without a second
  pid probe. It never launches a browser, never takes the store's write
  lock, and never mutates a record. More than
  ``_LIVE_SESSIONS_PER_OWNER_WARNING_THRESHOLD`` live sessions under one
  owner is reported as ``severity="warning"`` (the same threshold
  colleague#436 uses for its own doctor-style check) but, like every other
  advisory check here, never flips ``passed`` to ``False`` — a full session
  store is a hygiene signal, not a broken host.

Severity vs. ``passed`` — a deliberate departure worth flagging
-----------------------------------------------------------------

For every check above except ``playwright_importable``, ``passed`` is always
``True``, regardless of what was actually found. This is intentional, not a
bug: ``doctor``'s ``healthy`` field is (and, per the identity checks'
existing characterization tests, must stay) a plain
``all(check["passed"] for check in checks)`` — there is no severity-aware
carve-out in that formula, and this module does not add one, because doing
so would also silently change the pre-existing ``skills_present`` check's
behavior (also ``severity="warning"``, but its failure is expected to flip
``healthy`` to ``False`` — see
``tests/test_characterization.py::test_diagnose_flags_missing_skills_dir``).

Task t14's acceptance criteria are explicit that a host which simply lacks a
browser, or whose kernel/AppArmor configuration restricts the sandbox, must
**not** be reported unhealthy — "browser-capability warnings must NOT flip
healthy to false". Given the shared, unmodifiable formula, the only way to
honor that is for these advisory checks to always report ``passed=True``:
``severity`` and ``message``/``remediation`` carry the real finding, exactly
as they would for an ``info`` check. Only ``playwright_importable`` can ever
flip ``healthy`` — matching the acceptance criterion "playwright_importable
failing IS an error since playwright is a core dep".

Every probe here is a small, separately monkeypatchable function
(``_resolve_chromium_path``, ``_read_playwright_version``,
``_read_sysctl_int``, ``_default_state_dir``) precisely so
``tests/test_doctor_browser.py`` can exercise every severity/finding
combination without needing a real (or a deliberately absent) browser
install.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
from pathlib import Path
from typing import Any

from webglass.adapters.session_store import FileSessionStore, default_sessions_dir
from webglass.sessions import SessionStatus

Check = dict[str, object]

__all__ = [
    "browser_checks",
    "check_playwright_importable",
    "check_chromium_installed",
    "check_playwright_version",
    "check_usable_sandbox",
    "check_state_dir_writable",
    "check_session_store_health",
]

#: colleague#436 warns above this many *live* sessions under one owner in its
#: own doctor-style check; matching the number here means an operator who
#: knows one threshold knows both, and a future owner-scoped refinement
#: (build plan task t7's owner token) can reuse it unchanged.
_LIVE_SESSIONS_PER_OWNER_WARNING_THRESHOLD = 10

#: Ubuntu 23.10+/24.04-class AppArmor restriction on unprivileged user
#: namespace creation — the condition this module's docstring and
#: ``webglass/adapters/playwright.py``'s ``SANDBOX_REMEDIATION`` describe.
_APPARMOR_RESTRICT_PATH = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
#: Kernel-wide toggle for unprivileged user namespace creation. ``0`` blocks
#: it outright, independent of any AppArmor policy.
_UNPRIVILEGED_USERNS_CLONE_PATH = Path("/proc/sys/kernel/unprivileged_userns_clone")

_SANDBOX_DOCS_POINTER = (
    "see webglass.adapters.playwright.SANDBOX_REMEDIATION and "
    "docs/specs/2026-08-07-implement-webglass-issue-1.md (claim c32) for the full "
    "explanation; NEVER pass allow_unsandboxed=True / --no-sandbox outside a "
    "trusted, host-controlled test harness"
)


def _check(check_id: str, *, ok: bool, severity: str, message: str, remediation: str = "") -> Check:
    """Build one rubric-shaped check dict.

    ``ok`` is the actual diagnosis (capability present/absent, value
    correct/incorrect). For ``severity == "error"`` checks, ``passed``
    mirrors ``ok`` directly. For every other severity, ``passed`` is always
    ``True`` — see the module docstring for why.
    """
    return {
        "id": check_id,
        "passed": ok if severity == "error" else True,
        "severity": severity,
        "message": message,
        "remediation": remediation,
    }


def _import_playwright_adapter() -> Any:
    """The one seam this module uses to reach Playwright — never imports it directly.

    ``webglass/adapters/playwright.py`` is the only module in ``webglass/``
    allowed to import the ``playwright`` package itself
    (``tests/test_import_boundaries.py`` enforces this everywhere else).
    Importing *that* module is how this file learns whether Playwright is
    installed at all: a missing ``playwright`` package makes this import
    raise ``ModuleNotFoundError``, which every caller below catches.
    """
    import webglass.adapters.playwright as adapter

    return adapter


def check_playwright_importable() -> Check:
    try:
        _import_playwright_adapter()
    except Exception as exc:  # pragma: no cover - exercised via monkeypatch in tests
        return _check(
            "playwright_importable",
            ok=False,
            severity="error",
            message=f"import playwright failed: {exc}",
            remediation=(
                "playwright is a declared core runtime dependency "
                "(pyproject.toml `dependencies`); reinstall the environment with "
                "`uv sync`, or `pip install 'playwright>=1.55,<2'` outside uv, "
                "then re-run doctor"
            ),
        )
    return _check(
        "playwright_importable",
        ok=True,
        severity="error",
        message="playwright package imports cleanly",
    )


_ASYNCIO_LOGGER_QUIETED = False


def _quiet_playwright_teardown_chatter() -> None:
    """Silence the ``asyncio`` logger for the remainder of this process.

    ``chromium_executable_path()`` starts Playwright's driver — a background
    thread running its own asyncio event loop. That loop's pending-task
    cleanup logs ``Task was destroyed but it is pending!`` /
    ``TargetClosedError`` at interpreter-shutdown/garbage-collection time,
    observably *after* the call that started the driver has already
    returned (confirmed directly: a marker printed right after the
    triggering call still runs before this chatter appears). A
    context-managed, restore-afterwards suppression therefore cannot catch
    it — only a for-the-rest-of-the-process one can, which is exactly what
    ``webglass/adapters/playwright.py``'s module docstring anticipates:
    "whichever layer wires these operations into ``webglass`` verbs has to
    quiet the ``asyncio`` logger for the process." ``doctor`` is the first
    such layer (M0-M2's CLI verbs still run over fakes as of this task), so
    it does that quieting once, here, guarded so a repeat call is a no-op.
    """
    global _ASYNCIO_LOGGER_QUIETED
    if not _ASYNCIO_LOGGER_QUIETED:
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        _ASYNCIO_LOGGER_QUIETED = True


def _resolve_chromium_path() -> str:
    """Isolated seam so tests can fake "chromium missing" without a real browser."""
    adapter = _import_playwright_adapter()
    _quiet_playwright_teardown_chatter()
    return str(adapter.chromium_executable_path())


def check_chromium_installed() -> Check:
    try:
        path = _resolve_chromium_path()
    except Exception as exc:
        return _check(
            "chromium_installed",
            ok=False,
            severity="warning",
            message=f"could not resolve the Chromium binary path: {exc}",
            remediation=(
                "run `uv run playwright install chromium` (requires "
                "playwright_importable to pass first)"
            ),
        )
    exists = Path(path).is_file()
    return _check(
        "chromium_installed",
        ok=exists,
        severity="warning",
        message=(
            f"chromium binary found at {path}" if exists else f"chromium binary not found at {path}"
        ),
        remediation="" if exists else "run `uv run playwright install chromium`",
    )


def _read_playwright_version() -> str:
    adapter = _import_playwright_adapter()
    return str(adapter.playwright_version())


def _pinned_playwright_range() -> str:
    """The playwright requirement string as declared in ``pyproject.toml``.

    Read back through installed package metadata (``importlib.metadata``)
    rather than parsing ``pyproject.toml`` off disk, so this stays accurate
    from a wheel install (no ``pyproject.toml`` alongside the package) and
    from a source checkout alike.
    """
    try:
        requirements = importlib.metadata.requires("webglass-cli") or ()
    except importlib.metadata.PackageNotFoundError:
        return "unknown (package metadata unavailable)"
    for requirement in requirements:
        name_and_range = requirement.split(";", 1)[0].strip()
        if name_and_range.split()[0].lower().startswith("playwright"):
            return name_and_range
    return "unknown (no playwright requirement declared)"


def check_playwright_version() -> Check:
    try:
        version = _read_playwright_version()
    except Exception as exc:
        return _check(
            "playwright_version",
            ok=False,
            severity="info",
            message=f"could not determine the installed playwright version: {exc}",
        )
    return _check(
        "playwright_version",
        ok=True,
        severity="info",
        message=f"playwright {version} installed (pinned range: {_pinned_playwright_range()})",
    )


def _read_sysctl_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def check_usable_sandbox() -> Check:
    """Never attempts a launch — this is a passive ``/proc/sys`` read only.

    Detecting "no usable sandbox" by actually launching Chromium would mean
    either accepting the crash this condition causes, or passing
    ``--no-sandbox`` to probe around it — exactly the silent-downgrade this
    project refuses to do (see ``webglass/adapters/playwright.py``'s module
    docstring). Reading the two kernel switches the real launch failure is
    already known to correlate with keeps this check honest without ever
    touching ``--no-sandbox``.
    """
    apparmor_restricted = _read_sysctl_int(_APPARMOR_RESTRICT_PATH)
    userns_clone = _read_sysctl_int(_UNPRIVILEGED_USERNS_CLONE_PATH)

    restricted = apparmor_restricted == 1
    disabled = userns_clone == 0

    if restricted or disabled:
        reasons = []
        if restricted:
            reasons.append(f"{_APPARMOR_RESTRICT_PATH.name}=1")
        if disabled:
            reasons.append(f"{_UNPRIVILEGED_USERNS_CLONE_PATH.name}=0")
        return _check(
            "usable_sandbox",
            ok=False,
            severity="warning",
            message=(
                "unprivileged user namespaces are restricted "
                f"({', '.join(reasons)}); a default sandboxed Chromium launch will "
                "likely abort with 'No usable sandbox!'"
            ),
            remediation=(
                "this is normally AppArmor restricting unprivileged user namespaces "
                "on Ubuntu 23.10+/24.04-class hosts; " + _SANDBOX_DOCS_POINTER
            ),
        )
    if apparmor_restricted is None and userns_clone is None:
        return _check(
            "usable_sandbox",
            ok=True,
            severity="warning",
            message=(
                f"cannot read {_APPARMOR_RESTRICT_PATH} or {_UNPRIVILEGED_USERNS_CLONE_PATH} "
                "on this platform (likely not Linux); sandbox availability is undetermined"
            ),
        )
    return _check(
        "usable_sandbox",
        ok=True,
        severity="warning",
        message=(
            "unprivileged user namespaces are not restricted; a sandboxed Chromium "
            "launch should start"
        ),
    )


def _default_state_dir() -> Path:
    """``$XDG_STATE_HOME/webglass``, falling back to ``~/.local/state/webglass``.

    See the module docstring: this is a doctor-only environment probe, not a
    commitment to where the M3 SQLite/artifact stores will eventually live.
    """
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / "webglass"


def check_state_dir_writable(state_dir: Path | None = None) -> Check:
    directory = state_dir if state_dir is not None else _default_state_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return _check(
            "state_dir_writable",
            ok=False,
            severity="info",
            message=f"state dir {directory} is not writable: {exc}",
            remediation=f"ensure {directory} (or $XDG_STATE_HOME) is creatable and writable",
        )
    return _check(
        "state_dir_writable",
        ok=True,
        severity="info",
        message=f"state dir {directory} is creatable and writable",
    )


def _directory_size_bytes(directory: Path) -> int:
    """Total bytes of every regular file under ``directory``, or 0 if absent.

    A read-only ``rglob`` walk — no listing is cached, nothing is opened for
    write, and a directory that does not exist yet (no session has ever been
    created) is a normal, zero-size state rather than an error.
    """
    if not directory.is_dir():
        return 0
    return sum(entry.stat().st_size for entry in directory.rglob("*") if entry.is_file())


def check_session_store_health(sessions_dir: Path | None = None) -> Check:
    """Read-only live/stale/size/corruption survey of the session-record store.

    See the module docstring for the full rationale. Built entirely on
    :class:`~webglass.adapters.session_store.FileSessionStore`'s read paths
    (:meth:`~webglass.adapters.session_store.FileSessionStore.list`,
    :meth:`~webglass.adapters.session_store.FileSessionStore.list_corrupt`) —
    neither takes the store's write lock, launches a browser, or mutates a
    record, satisfying this check's own "read-only and cheap" acceptance
    criterion.

    "Live" is an ``active`` record whose ``observed_liveness`` (computed
    fresh from a ``kill(pid, 0)`` probe, task t3) is ``"running"``; every
    other ``active`` record — dead pid, foreign pid, or no pid at all — is
    "stale": exactly the "records marked active with dead pids read as live
    forever" defect issue #14 reported, because nothing today sweeps them.
    ``closed``/``expired`` records are counted in the total but are neither
    live nor stale — their status already says what happened to them.

    Owner-scoping today is a plain ``owner`` field group-by; build plan task
    t7 adds a dedicated owner-token field in parallel, and this function is
    written so that refining "for one owner" to that field later only
    changes the grouping key, not this check's shape.
    """
    store = FileSessionStore(sessions_dir if sessions_dir is not None else default_sessions_dir())
    records = store.list()
    corrupt = store.list_corrupt()

    live_by_owner: dict[str, int] = {}
    stale_count = 0
    for record in records:
        if record.status is not SessionStatus.ACTIVE:
            continue
        if record.to_public_dict()["observed_liveness"] == "running":
            live_by_owner[record.owner] = live_by_owner.get(record.owner, 0) + 1
        else:
            stale_count += 1

    live_count = sum(live_by_owner.values())
    busiest_owner, busiest_owner_live = max(
        live_by_owner.items(), key=lambda item: item[1], default=("", 0)
    )
    over_threshold = busiest_owner_live > _LIVE_SESSIONS_PER_OWNER_WARNING_THRESHOLD
    directory_size = _directory_size_bytes(store.directory)

    message_parts = [
        f"{len(records)} session record(s) ({live_count} live, {stale_count} stale)",
        f"record directory size: {directory_size} bytes",
    ]
    if over_threshold:
        message_parts.append(
            f"owner '{busiest_owner}' has {busiest_owner_live} live sessions "
            f"(> {_LIVE_SESSIONS_PER_OWNER_WARNING_THRESHOLD} threshold)"
        )
    if corrupt:
        message_parts.append(f"{len(corrupt)} corrupt record(s) skipped")

    return _check(
        "session_store_health",
        ok=True,
        severity="warning" if over_threshold else "info",
        message="; ".join(message_parts),
        remediation=(
            "run `webglass session clean` to sweep stale/expired sessions and reclaim disk space"
            if (over_threshold or stale_count or corrupt)
            else ""
        ),
    )


def browser_checks() -> list[Check]:
    """All t14+t12 browser-capability and session-store checks, in acceptance-criteria order."""
    return [
        check_playwright_importable(),
        check_chromium_installed(),
        check_playwright_version(),
        check_usable_sandbox(),
        check_state_dir_writable(),
        check_session_store_health(),
    ]
