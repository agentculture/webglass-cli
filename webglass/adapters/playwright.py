"""Playwright/Chromium :class:`~webglass.adapters.browser.BrowserBackend` (build plan task t11).

**This is the only module in ``webglass/`` allowed to import Playwright.**
Issue #1 section 12 and CLAUDE.md "Target architecture" section 8 keep
Playwright "the first backend, not the operation model": every other module
— ``operations``/``results``/``policy``/``extraction``/``evidence``/the CLI —
stays import-clean of it, and no Playwright type crosses a public signature
in this module either. ``tests/test_import_boundaries.py`` enforces the
import ban everywhere else; ``tests/test_playwright_adapter.py`` walks this
module's own public annotations and asserts none of them resolve into the
``playwright`` package. (Playwright itself is a *core* runtime dependency as
of the 2026-08-07 user decision, spec claim c8 — the ban is about coupling,
not about installability.)

Three public entry points, in the order a caller uses them:

- :func:`launch_detached` starts a **detached** headless Chromium — its own
  process group, its own ``--user-data-dir``, and ``--remote-debugging-port=0``
  so the OS picks the port. The actual port is read back from the
  ``DevToolsActivePort`` file Chromium writes into that directory. The
  returned :class:`DetachedBrowser` outlives the Python process that launched
  it; that is what makes a session reusable across separate one-shot CLI
  invocations (spec claim c29).
- :func:`connect` attaches to an already-running browser over CDP and returns
  a :class:`PlaywrightBrowserBackend`. **Connecting is not launching**: a
  fresh process can connect to a browser it never started, and the page's
  in-memory JavaScript state is still there. That property is the whole point
  of the challenge-pass probe this module's reattach test mirrors.
- :class:`PlaywrightBrowserBackend` implements the ``BrowserBackend``
  protocol (``open``/``press``/``screenshot``/``close``) speaking only URLs,
  session-id strings, bytes, and the local dataclasses defined in
  :mod:`webglass.adapters.browser`.

Task t12 builds session persistence on exactly these two functions: it
stores :attr:`DetachedBrowser.endpoint` as
:attr:`webglass.sessions.SessionRecord.endpoint_ref` and calls
:func:`connect` on a later invocation. **The endpoint is
secret-equivalent** — whoever holds the CDP URL owns the browser — so
:class:`DetachedBrowser` redacts it from ``repr()`` and omits it from
:meth:`DetachedBrowser.to_public_dict`, mirroring what ``SessionRecord``
already does.

Sandbox policy
--------------

The Chromium sandbox is **never** disabled implicitly. ``--no-sandbox``
appears in the argv only when a caller passes ``allow_unsandboxed=True``, and
that choice is loud: the launch is recorded as unsandboxed and every result
the resulting backend produces carries :data:`SANDBOX_DISABLED_WARNING` in
its WebGlass-authored ``diagnostics``. When the sandbox is unavailable and
the caller did *not* opt in, the launch raises :class:`BrowserLaunchError`
with actionable remediation — never a silent downgrade to a
semantically different (unsandboxed) browser.

Verified on this class of host (Ubuntu 23.10+/24.04-style AppArmor
restriction on unprivileged user namespaces): a default sandboxed launch
aborts with ``SIGABRT`` and ``FATAL:...zygote_host_impl_linux.cc: No usable
sandbox!``. That is a real, reproducible failure this module turns into a
structured error.

Known limits, declared rather than hidden
-----------------------------------------

- **Per-hop redirect policy revalidation is not implemented here.** A
  supplied :class:`~webglass.policy.WebPolicyEvaluator` is consulted before
  navigation, so a denied target is never contacted — that part is real
  enforcement. But Chromium follows 3xx hops internally and Playwright does
  *not* re-invoke a route handler for an auto-followed redirect target
  (probed directly: intercepting ``**/*`` saw only the initial navigation
  request of a two-hop chain). Rather than ship interception that fails open,
  this backend records every hop in ``redirect_chain`` and, whenever a policy
  is in force and a navigation actually redirected, stamps
  :data:`UNREVALIDATED_REDIRECTS_WARNING` into the result's ``diagnostics``.
  Real per-hop enforcement needs a mechanism that sees each hop before it is
  followed (a policy-enforcing local proxy, or manual redirect handling) —
  that is a later task's design decision, not a silent gap here.
- **One detached browser per session.** The session's working page is the
  single page in the connected browser's first context; browsers are launched
  with ``--no-startup-window`` so that context starts with zero pages and the
  page this backend creates is unambiguously the one a later process
  reattaches to. Multi-tab sessions are deferred.
- **``PressResult.key_log`` is WebGlass's own record** of the keys *this
  backend instance* dispatched — not a read-back of the page's
  ``window.__webglassKeyLog``. Reading page-authored state into a field that
  looks like tool provenance would cross the trust boundary issue #1
  section 7 draws; page-side key state is observable through
  :meth:`PlaywrightBrowserBackend.current` as ordinary (untrusted) page HTML.
  A consequence worth knowing: after reattaching from a *different* process,
  ``key_log`` starts empty again while the page's own in-memory log does not.
- **Playwright 1.62's sync API prints teardown chatter at interpreter exit**
  — ``Task was destroyed but it is pending!`` followed by a
  ``TargetClosedError`` traceback, logged through the ``asyncio`` logger.
  Reproducible with nothing but ``with sync_playwright() as p:
  p.chromium.executable_path`` and no WebGlass code at all, so it is not
  something this module can fix at the source. It matters anyway, because
  the CLI's error contract is that *no Python traceback ever reaches
  stderr*: whichever layer wires these operations into ``webglass`` verbs
  has to quiet the ``asyncio`` logger for the process. Keeping the driver
  start count low (hence the cache on :func:`chromium_executable_path`)
  limits the exposure but does not remove it.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as _PlaywrightError
from playwright.sync_api import sync_playwright as _sync_playwright

from webglass.adapters.browser import BrowserOpenResult, ConsoleMessage, PageError, PressResult
from webglass.policy import WebPolicyEvaluator
from webglass.results import NavigationHop

__all__ = [
    "BACKEND_ID",
    "SANDBOX_UNAVAILABLE_MARKERS",
    "SANDBOX_REMEDIATION",
    "SANDBOX_DISABLED_WARNING",
    "UNREVALIDATED_REDIRECTS_WARNING",
    "BrowserLaunchError",
    "DetachedBrowser",
    "PlaywrightOpenResult",
    "PlaywrightPressResult",
    "PlaywrightBrowserBackend",
    "chromium_executable_path",
    "connect",
    "launch_detached",
    "playwright_version",
]

#: Identifies this backend in results, diagnostics, and session records.
BACKEND_ID = "playwright-chromium"

#: Substrings Chromium prints when it cannot establish a usable sandbox.
#: Matched against the launch log to tell "the sandbox is unavailable on this
#: host" apart from every other reason a browser might fail to start.
SANDBOX_UNAVAILABLE_MARKERS = (
    "No usable sandbox",
    "Running as root without --no-sandbox",
)

SANDBOX_REMEDIATION = (
    "Chromium could not establish a usable sandbox. On Ubuntu 23.10+/24.04-class "
    "hosts this is normally AppArmor restricting unprivileged user namespaces: "
    "check `sysctl kernel.apparmor_restrict_unprivileged_userns` (1 means restricted) "
    "and either install an AppArmor profile for the Playwright Chromium binary or "
    "run in a container/CI image that permits user namespaces. Only a trusted test "
    "harness on a host you control should pass allow_unsandboxed=True, which adds "
    "--no-sandbox and marks every result as sandbox-disabled."
)

#: Stamped into the ``diagnostics`` of every result produced through a browser
#: that was launched with ``--no-sandbox``. WebGlass-authored trusted text.
SANDBOX_DISABLED_WARNING = (
    "sandbox-disabled: this browser was launched with --no-sandbox by explicit "
    "caller opt-in (allow_unsandboxed=True); page content ran without the "
    "Chromium sandbox"
)

#: Stamped when a policy is in force and a navigation followed redirect hops
#: that this backend could not revalidate before they were followed.
UNREVALIDATED_REDIRECTS_WARNING = (
    "redirect-hops-not-revalidated: the browser followed one or more redirects "
    "internally; this backend evaluated policy only on the requested URL, and "
    "the intermediate hops are reported in redirect_chain after the fact"
)

_DEVTOOLS_PORT_FILE = "DevToolsActivePort"
_LAUNCH_POLL_SECONDS = 0.05
_DEFAULT_LAUNCH_TIMEOUT_SECONDS = 30.0
_DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000.0
#: How long to let queued console/pageerror events drain after an action.
#: Playwright delivers them over the same driver channel as the command that
#: caused them, so a short settle is what makes "the throwing fixture reports
#: a page error" reliable rather than racy.
_DEFAULT_EVENT_SETTLE_MS = 100.0
_LOG_TAIL_CHARS = 4000

#: Chromium flags every WebGlass launch uses. ``--no-startup-window`` is
#: load-bearing (see the module docstring): it keeps the default context empty
#: so the page this backend creates is the one a later process reattaches to.
#: The ``--disable-*`` flags suppress Chromium's own background network
#: chatter, which would otherwise contact real hosts from a test run that is
#: supposed to touch nothing but the local fixture site.
_BASE_CHROMIUM_ARGS = (
    "--headless=new",
    "--remote-debugging-port=0",
    "--no-startup-window",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
)

# "    at http://host:port/path:16:13" / "    at fn (http://.../x.js:1:2)"
_STACK_FRAME = re.compile(r"at (?:[^()]*\()?([a-z][a-z0-9+.-]*:[^\s()]+?):(\d+):(\d+)\)?", re.I)


class BrowserLaunchError(RuntimeError):
    """A browser could not be launched, reported as structured data.

    Carries the same ``code``/``message``/``remediation`` triple as
    :class:`webglass.results.OperationError` and the CLI's ``CliError`` so a
    caller can render it through the existing ``error:`` / ``hint:`` text
    contract or drop it straight into a JSON result — without this module
    importing either of those layers.

    ``code`` is stable and machine-readable:
    ``browser_sandbox_unavailable`` (the host blocks the Chromium sandbox and
    the caller did not opt out), ``browser_launch_failed`` (the process died
    for some other reason), or ``browser_launch_timeout``.
    """

    def __init__(self, code: str, message: str, remediation: str = "", log_tail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        #: Untrusted-ish backend output: Chromium's own stderr. Useful for
        #: diagnosis, never treated as a WebGlass assertion about the world.
        self.log_tail = log_tail

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class DetachedBrowser:
    """A running browser process this Python process does not own.

    :attr:`endpoint` is **secret-equivalent** (spec claim c33): the CDP URL
    grants full control of the browser, so it is excluded from ``repr()`` and
    from :meth:`to_public_dict` exactly as
    :class:`webglass.sessions.SessionRecord` excludes ``endpoint_ref``.

    Reconstructible from stored fields alone: t12 can rebuild one of these
    from a session record in a *different* process and still
    :meth:`terminate` it, because termination goes through :attr:`pid` rather
    than through a :class:`subprocess.Popen` handle this process happens to
    hold.
    """

    endpoint: str
    pid: int
    user_data_dir: str
    sandboxed: bool = True
    diagnostics: tuple[str, ...] = ()
    #: Present only in the process that launched the browser; termination
    #: works without it.
    process: Any = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"DetachedBrowser(endpoint=<redacted>, pid={self.pid!r}, "
            f"user_data_dir={self.user_data_dir!r}, sandboxed={self.sandboxed!r}, "
            f"diagnostics={self.diagnostics!r})"
        )

    def to_public_dict(self) -> dict[str, Any]:
        """A dict safe for JSON output, logs, or evidence: no ``endpoint``."""
        return {
            "pid": self.pid,
            "user_data_dir": self.user_data_dir,
            "sandboxed": self.sandboxed,
            "diagnostics": list(self.diagnostics),
        }

    def is_running(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def terminate(self, *, timeout_seconds: float = 5.0) -> None:
        """Stop the browser process, escalating to ``SIGKILL`` if needed.

        Idempotent: terminating an already-dead browser is not an error.
        Deliberately minimal — deterministic *session* cleanup (leases,
        expiry, reaping orphans) is task t12's contract, not this dataclass's.
        """
        try:
            os.kill(self.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.is_running():
                break
            time.sleep(_LAUNCH_POLL_SECONDS)
        else:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        process = self.process
        if process is not None:
            # Reap the zombie when this process is the parent. Best-effort:
            # a browser terminated from a *different* process has no handle
            # here at all, and that path must work identically.
            with suppress(subprocess.SubprocessError, OSError):
                process.wait(timeout=timeout_seconds)


@dataclass(frozen=True)
class PlaywrightOpenResult(BrowserOpenResult):
    """:class:`BrowserOpenResult` plus WebGlass-authored ``diagnostics``.

    A structural subclass, so every conformance assertion written against
    ``BrowserOpenResult`` holds unchanged. ``diagnostics`` is *trusted control
    metadata* (issue #1 section 7) — sandbox state, navigation failures,
    declared omissions — and is deliberately a separate field from
    ``console_messages``/``page_errors``/``html``, which carry text the page
    authored. Remote text must never be able to appear where a WebGlass
    warning appears.
    """

    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["diagnostics"] = list(self.diagnostics)
        return payload


@dataclass(frozen=True)
class PlaywrightPressResult(PressResult):
    """:class:`PressResult` plus WebGlass-authored ``diagnostics``."""

    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["diagnostics"] = list(self.diagnostics)
        return payload


def playwright_version() -> str:
    """The installed Playwright version — reported by ``doctor`` at t14."""
    from importlib.metadata import version

    return version("playwright")


@lru_cache(maxsize=1)
def chromium_executable_path() -> str:
    """Absolute path of the Chromium binary Playwright would launch.

    Resolved through Playwright itself (so it always names the revision this
    Playwright version is pinned to) but returned as a plain ``str`` — no
    Playwright object escapes. Cached because answering it costs a full
    driver (Node subprocess) start, which would otherwise be paid again on
    every :func:`launch_detached` call.
    """
    with _sync_playwright() as playwright:
        return str(playwright.chromium.executable_path)


def _read_devtools_endpoint(user_data_dir: Path) -> str | None:
    """Return the CDP endpoint from ``DevToolsActivePort``, or ``None``.

    Chromium writes two lines: the port it actually bound (we always ask for
    ``0``, i.e. "pick one", so this file is the only way to learn it) and the
    browser-level websocket path. ``connect_over_cdp`` accepts the plain
    ``http://host:port`` form, which is what a stored session endpoint holds.
    """
    port_file = user_data_dir / _DEVTOOLS_PORT_FILE
    try:
        lines = port_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or not lines[0].strip().isdigit():
        return None
    return f"http://127.0.0.1:{lines[0].strip()}"


def _classify_launch_failure(log_tail: str, returncode: int | None) -> BrowserLaunchError:
    if any(marker in log_tail for marker in SANDBOX_UNAVAILABLE_MARKERS):
        return BrowserLaunchError(
            code="browser_sandbox_unavailable",
            message=(
                "Chromium exited without a usable sandbox "
                f"(exit status {returncode}); refusing to fall back to --no-sandbox"
            ),
            remediation=SANDBOX_REMEDIATION,
            log_tail=log_tail,
        )
    return BrowserLaunchError(
        code="browser_launch_failed",
        message=f"Chromium exited before reporting a DevTools endpoint (exit status {returncode})",
        remediation=(
            "Run `webglass doctor` for browser diagnostics, confirm the pinned Chromium is "
            "installed (`playwright install chromium`), and inspect the launch log in the "
            "browser's user-data-dir."
        ),
        log_tail=log_tail,
    )


def launch_detached(
    user_data_dir: str | os.PathLike[str],
    *,
    allow_unsandboxed: bool = False,
    extra_args: Sequence[str] = (),
    timeout_seconds: float = _DEFAULT_LAUNCH_TIMEOUT_SECONDS,
    executable_path: str | None = None,
) -> DetachedBrowser:
    """Launch a headless Chromium that outlives this process.

    The browser is started in its own session/process group
    (``start_new_session=True``) with its own profile directory, so the
    calling process can exit — or crash — without taking the browser with it.
    That is the mechanism behind sessions surviving between one-shot CLI
    invocations.

    ``allow_unsandboxed`` is the *only* way ``--no-sandbox`` ever reaches the
    argv, and using it is recorded: the returned browser reports
    ``sandboxed=False`` and carries :data:`SANDBOX_DISABLED_WARNING` in its
    diagnostics, which :func:`connect` propagates into every result. Intended
    for a trusted test harness on a host whose kernel/AppArmor configuration
    blocks the sandbox — never as an automatic fallback.

    Raises :class:`BrowserLaunchError` (``browser_sandbox_unavailable`` /
    ``browser_launch_failed`` / ``browser_launch_timeout``) if no endpoint
    appears; never a bare ``CalledProcessError`` or a Playwright exception.
    """
    directory = Path(user_data_dir)
    # Chromium writes DevToolsActivePort (the secret-equivalent CDP endpoint)
    # inside this directory, so its permissions cannot be left to the umask —
    # and a pre-existing directory must be corrected, not trusted.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    log_path = directory / "chromium-launch.log"
    # A browser that was killed rather than closed leaves its
    # DevToolsActivePort behind. Reusing a profile directory would then hand
    # back a *dead* endpoint that looks perfectly valid — remove it first so
    # the only file we can read is the one this launch writes.
    (directory / _DEVTOOLS_PORT_FILE).unlink(missing_ok=True)

    argv = [executable_path or chromium_executable_path()]
    argv.extend(_BASE_CHROMIUM_ARGS)
    argv.append(f"--user-data-dir={directory}")
    if allow_unsandboxed:
        argv.append("--no-sandbox")
    argv.extend(extra_args)

    def _log_tail() -> str:
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")[-_LOG_TAIL_CHARS:]
        except OSError:  # pragma: no cover - unreadable log is not the failure
            return ""

    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(  # nosec B603 - argv list, no shell, path from Playwright
            argv,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        endpoint = _read_devtools_endpoint(directory)
        if endpoint is not None:
            diagnostics = (SANDBOX_DISABLED_WARNING,) if allow_unsandboxed else ()
            return DetachedBrowser(
                endpoint=endpoint,
                pid=process.pid,
                user_data_dir=str(directory),
                sandboxed=not allow_unsandboxed,
                diagnostics=diagnostics,
                process=process,
            )
        returncode = process.poll()
        if returncode is not None:
            raise _classify_launch_failure(_log_tail(), returncode)
        time.sleep(_LAUNCH_POLL_SECONDS)

    process.kill()
    # Reap the killed child so a long-lived caller that retries launches never
    # accumulates zombies; best-effort — the raise below must not be masked.
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=5)
    raise BrowserLaunchError(
        code="browser_launch_timeout",
        message=(
            f"Chromium did not report a DevTools endpoint within {timeout_seconds:g}s; "
            "the process was killed"
        ),
        remediation=(
            "Raise timeout_seconds for a slow or heavily loaded host, or run "
            "`webglass doctor` to check the browser installation."
        ),
        log_tail=_log_tail(),
    )


def connect(
    endpoint: str,
    *,
    policy: WebPolicyEvaluator | None = None,
    diagnostics: Sequence[str] = (),
    navigation_timeout_ms: float = _DEFAULT_NAVIGATION_TIMEOUT_MS,
    event_settle_ms: float = _DEFAULT_EVENT_SETTLE_MS,
) -> PlaywrightBrowserBackend:
    """Attach to a browser already running at ``endpoint`` over CDP.

    The counterpart to :func:`launch_detached`, and the function t12's
    session store calls with a stored ``endpoint_ref``. Connecting does not
    launch anything and does not reset anything: pages, cookies, and
    **in-memory JavaScript state** are exactly as the previous client left
    them, even if that client was a different process that has since exited.

    Pass ``diagnostics=browser.diagnostics`` to carry a sandbox-disabled
    marker from the launch into every result this backend produces.
    """
    return PlaywrightBrowserBackend(
        endpoint,
        policy=policy,
        diagnostics=diagnostics,
        navigation_timeout_ms=navigation_timeout_ms,
        event_settle_ms=event_settle_ms,
    )


class _SessionState:
    """Per-session live objects. Private: nothing here crosses the seam."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.playwright: Any = None
        self.browser: Any = None
        self.page: Any = None
        self.console: list[ConsoleMessage] = []
        self.errors: list[PageError] = []
        self.key_log: list[str] = []
        self.last_status: int | None = None


class PlaywrightBrowserBackend:
    """A :class:`~webglass.adapters.browser.BrowserBackend` over live Chromium.

    Construct it with an endpoint resolver — a single endpoint string (one
    browser, every session id maps to it), a mapping of session id to
    endpoint, or a callable resolving one. That callable is the seam t12
    uses: it hands over a function that looks the session up in the on-disk
    session store and returns its ``endpoint_ref``, so this class never needs
    to know how sessions are stored (see the ``browser.py`` module docstring:
    launch/connect lifecycle is deliberately *not* part of the protocol).

    Connections are opened lazily, on first use of a session id, and reused.
    Nothing here launches a browser; :func:`launch_detached` does that.
    """

    def __init__(
        self,
        endpoints: str | Mapping[str, str] | Callable[[str], str],
        *,
        policy: WebPolicyEvaluator | None = None,
        diagnostics: Sequence[str] = (),
        navigation_timeout_ms: float = _DEFAULT_NAVIGATION_TIMEOUT_MS,
        event_settle_ms: float = _DEFAULT_EVENT_SETTLE_MS,
        backend_id: str = BACKEND_ID,
    ) -> None:
        self._endpoints = endpoints
        self._policy = policy
        self._diagnostics = tuple(diagnostics)
        self._navigation_timeout_ms = navigation_timeout_ms
        self._event_settle_ms = event_settle_ms
        self.backend_id = backend_id
        self._sessions: dict[str, _SessionState] = {}

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> PlaywrightBrowserBackend:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disconnect()

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """WebGlass-authored diagnostics stamped onto every result."""
        return self._diagnostics

    def endpoint_for(self, session_id: str) -> str:
        """Resolve ``session_id`` to a CDP endpoint (secret-equivalent)."""
        endpoints = self._endpoints
        if isinstance(endpoints, str):
            return endpoints
        if isinstance(endpoints, Mapping):
            try:
                return endpoints[session_id]
            except KeyError:
                raise KeyError(f"no endpoint registered for session_id: {session_id}") from None
        return endpoints(session_id)

    def disconnect(self) -> None:
        """Drop every CDP connection, leaving the browsers themselves running.

        The inverse of :func:`connect`, and *not* the inverse of
        :func:`launch_detached`: a detached browser survives this call, which
        is precisely what lets a later process reattach and find its pages
        intact. Killing a browser is :meth:`DetachedBrowser.terminate`.
        """
        # The list() is a snapshot, not a redundant conversion: _release()
        # pops from self._sessions as we iterate it.
        for session_id in list(self._sessions):  # NOSONAR(S7504)
            self._release(session_id)

    def _release(self, session_id: str) -> None:
        state = self._sessions.pop(session_id, None)
        if state is None:
            return
        # On a CDP *connection* (as opposed to a browser Playwright launched
        # itself) closing the browser object disconnects the client and
        # leaves the remote browser running — verified directly: after this,
        # the detached process is still alive and its pages still hold their
        # in-memory JavaScript state. Stopping the driver afterwards releases
        # the Node subprocess this connection was using.
        # Teardown is best-effort by design: a connection whose remote
        # browser already went away must still release the local driver
        # rather than propagate a failure out of a cleanup path.
        if state.browser is not None:
            with suppress(Exception):
                state.browser.close()
        if state.playwright is not None:
            with suppress(Exception):
                state.playwright.stop()

    # -- BrowserBackend ----------------------------------------------------

    def open(self, session_id: str, url: str) -> PlaywrightOpenResult:
        """Navigate ``session_id``'s page to ``url`` and report what happened.

        Never raises for an ordinary web failure: an unreachable host, a
        navigation timeout, or a policy denial all come back as a structured
        result with ``html=""``. Console messages and page errors observed
        during the navigation are captured as untrusted page-authored text.
        """
        if self._policy is not None:
            verdict = self._policy.evaluate(url, hop_index=0)
            if not verdict.allowed:
                # Denied before any request is issued — the target is never
                # contacted, which is the only honest meaning of "blocked".
                return PlaywrightOpenResult(
                    requested_url=url,
                    final_url=url,
                    status=None,
                    redirect_chain=(NavigationHop(requested_url=url),),
                    blocked=True,
                    policy_verdict=verdict,
                    diagnostics=self._diagnostics,
                )

        state = self._state(session_id)
        page = state.page
        state.console.clear()
        state.errors.clear()

        try:
            response = page.goto(url, timeout=self._navigation_timeout_ms, wait_until="load")
        except _PlaywrightError as exc:
            # Chromium commits an error page a beat after goto() rejects;
            # settling here keeps the *next* open() on this session from
            # failing with "interrupted by another navigation".
            self._settle(page)
            state.last_status = None
            return PlaywrightOpenResult(
                requested_url=url,
                final_url=url,
                status=None,
                redirect_chain=(NavigationHop(requested_url=url),),
                console_messages=tuple(state.console),
                page_errors=tuple(state.errors),
                diagnostics=self._diagnostics + (f"navigation-failed: {_first_line(exc)}",),
            )

        self._settle(page)
        chain = _redirect_chain(response, url)
        status = response.status if response is not None else None
        state.last_status = status
        diagnostics = self._diagnostics
        if self._policy is not None and len(chain) > 1:
            diagnostics = diagnostics + (UNREVALIDATED_REDIRECTS_WARNING,)
        return PlaywrightOpenResult(
            requested_url=url,
            final_url=response.url if response is not None else page.url,
            status=status,
            html=_content(page),
            redirect_chain=chain,
            console_messages=tuple(state.console),
            page_errors=tuple(state.errors),
            diagnostics=diagnostics,
        )

    def press(
        self, session_id: str, keys: Sequence[str], delay_ms: float = 0
    ) -> PlaywrightPressResult:
        """Dispatch ``keys`` in order to the session's focused page.

        ``key_log`` accumulates what *this backend instance* has dispatched
        (see the module docstring on why it is not read back out of the
        page). Page-side effects of the keys are observable through
        :meth:`current`.
        """
        state = self._state(session_id)
        page = state.page
        pressed = tuple(keys)
        for index, key in enumerate(pressed):
            if delay_ms and index:
                page.wait_for_timeout(delay_ms)
            page.keyboard.press(key)
        state.key_log.extend(pressed)
        self._settle(page)
        return PlaywrightPressResult(
            session_id=session_id,
            pressed=pressed,
            key_log=tuple(state.key_log),
            diagnostics=self._diagnostics,
        )

    def screenshot(self, session_id: str) -> bytes:
        """Capture the session's current page as PNG bytes."""
        return bytes(self._state(session_id).page.screenshot(type="png"))

    def close(self, session_id: str) -> None:
        """Close the session's page and drop this backend's connection to it.

        Does **not** stop the detached browser process — session lifetime
        (leases, expiry, reaping) belongs to the session store, not to a
        backend method that a single verb might call.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return
        if state.page is not None:
            with suppress(Exception):  # closing an already-dead page is fine
                state.page.close()
        self._release(session_id)

    # -- beyond the protocol ----------------------------------------------

    def current(self, session_id: str) -> PlaywrightOpenResult:
        """Re-read the session's page **without navigating**.

        The protocol's ``open`` always navigates, which destroys exactly the
        in-memory page state a reattached session exists to preserve. This
        method reads the live DOM as it stands — the only way to observe what
        a ``press`` (or a previous process's work) did to a page. ``status``
        is carried over from the last navigation this backend performed and
        is ``None`` after a reattach, because this process never saw that
        response.
        """
        state = self._state(session_id)
        page = state.page
        return PlaywrightOpenResult(
            requested_url=page.url,
            final_url=page.url,
            status=state.last_status,
            html=_content(page),
            redirect_chain=(NavigationHop(requested_url=page.url, response_url=page.url),),
            console_messages=tuple(state.console),
            page_errors=tuple(state.errors),
            diagnostics=self._diagnostics,
        )

    # -- internals ---------------------------------------------------------

    def _settle(self, page: Any) -> None:
        if self._event_settle_ms:
            try:
                page.wait_for_timeout(self._event_settle_ms)
            except _PlaywrightError:  # pragma: no cover - page went away
                pass

    def _state(self, session_id: str) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is not None:
            return state
        state = _SessionState(self.endpoint_for(session_id))
        self._sessions[session_id] = state
        try:
            state.playwright = _sync_playwright().start()
            state.browser = state.playwright.chromium.connect_over_cdp(state.endpoint)
            state.page = self._adopt_page(state.browser)
            self._instrument(state)
        except BaseException:
            self._release(session_id)
            raise
        return state

    def _adopt_page(self, browser: Any) -> Any:
        """Find this session's page, or create it.

        Browsers are launched with ``--no-startup-window``, so a freshly
        launched browser has zero pages and this creates one; a *reattached*
        browser has exactly the page the previous client left behind, and
        this adopts it — with its in-memory JavaScript state intact.
        ``chrome://`` and ``about:blank`` pages are skipped so a stray
        new-tab page can never be mistaken for the session's work.
        """
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        for page in context.pages:
            url = page.url or ""
            if not url.startswith(("chrome://", "chrome-extension://", "about:")):
                return page
        return context.new_page()

    def _instrument(self, state: _SessionState) -> None:
        """Attach console/page-error capture and answer the favicon probe.

        The favicon route is not cosmetic: Chromium requests ``/favicon.ico``
        on every navigation and logs a console *error* when the site 404s it,
        so without this every clean page would carry a phantom console error
        that the page never produced. Answering it locally with ``204`` keeps
        "this page logged nothing" a true statement instead of a noisy one.
        """
        page = state.page
        page.on("console", lambda message: state.console.append(_console_message(message)))
        page.on("pageerror", lambda error: state.errors.append(_page_error(error)))
        page.context.route("**/favicon.ico", lambda route: route.fulfill(status=204, body=b""))


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__


def _content(page: Any) -> str:
    """Serialize the live DOM, tolerating a page that is mid-navigation."""
    try:
        return str(page.content())
    except _PlaywrightError:
        return ""


def _console_message(message: Any) -> ConsoleMessage:
    location = message.location or {}
    return ConsoleMessage(
        level=str(message.type),
        text=str(message.text),
        source_url=location.get("url") or None,
        line=location.get("lineNumber"),
    )


def _page_error(error: Any) -> PageError:
    name = getattr(error, "name", "") or "Error"
    message = getattr(error, "message", "") or str(error)
    stack = getattr(error, "stack", "") or ""
    source_url, line = _parse_stack_location(stack)
    return PageError(text=f"{name}: {message}", source_url=source_url, line=line)


def _parse_stack_location(stack: str) -> tuple[str | None, int | None]:
    """Pull ``(source_url, line)`` out of the first frame of a JS stack.

    Chromium formats an uncaught error's stack as
    ``Error: msg\\n    at http://host/page:16:13``. The source location is
    only available this way — Playwright's ``pageerror`` payload has no
    structured location field — and a page error without a source location
    would be much weaker evidence.
    """
    match = _STACK_FRAME.search(stack or "")
    if match is None:
        return None, None
    return match.group(1), int(match.group(2))


def _redirect_chain(response: Any, requested_url: str) -> tuple[NavigationHop, ...]:
    """Report a navigation hop by hop, oldest first — never collapsed.

    Rebuilt by walking ``request.redirected_from`` back to the original
    request, so each hop names the URL that was asked for, the URL it led to,
    and the status that said so.
    """
    if response is None:
        return (NavigationHop(requested_url=requested_url),)
    requests: list[Any] = []
    request: Any = response.request
    while request is not None:
        requests.append(request)
        request = request.redirected_from
    requests.reverse()

    hops: list[NavigationHop] = []
    for index, hop_request in enumerate(requests):
        hop_response = hop_request.response()
        next_url = requests[index + 1].url if index + 1 < len(requests) else hop_request.url
        hops.append(
            NavigationHop(
                requested_url=hop_request.url,
                response_url=next_url,
                status=hop_response.status if hop_response is not None else None,
            )
        )
    return tuple(hops) or (NavigationHop(requested_url=requested_url),)
