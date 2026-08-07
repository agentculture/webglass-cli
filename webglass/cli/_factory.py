"""CLI adapter-wiring seam — the one place CLI handlers get a WebGlassService.

CLAUDE.md's CLI skeleton section fixes the handler contract: a handler builds
a ``WebOperation`` + ``WebContext``, hands both to
:meth:`~webglass.service.WebGlassService.execute`, and renders the result —
*no operation logic in the handler itself* (spec honesty h2). This module is
the other half of that contract: it is the single place that constructs the
``WebGlassService`` and the ``WebContext`` a CLI invocation runs against, plus
the shared ``WebOperation``-building and result-rendering helpers every noun
module (``search.py``, ``page.py``, ``action.py``, ``session.py``) calls — so
no ``_commands`` module ever imports an adapter, constructs a
``WebGlassService`` directly, or duplicates the exit-code/output mapping.

Default posture
----------------
:func:`build_service` wires a real :class:`Clock`/:class:`IdProvider` (this
module's :class:`SystemClock`/:class:`UuidIds`) and a
:class:`~webglass.adapters.session_store.FileSessionStore`, and deliberately
injects **no** search or artifact backend. That is not an oversight:
:class:`~webglass.service.WebGlassService` already turns a missing adapter
into a structured ``backend_unavailable`` result rather than an
``AttributeError`` or a silent substitution, so ``webglass search``/``page``/
``action`` verbs round-trip through the full CLI/JSON/exit-code contract
today — they just correctly report that the backend capability does not
exist at this milestone. The search provider lands at t15; this module is the
seam it wires through.

The browser backend is different: it exists (t11's Playwright adapter) and is
wired here, but behind a **staging switch**, :data:`BROWSER_BACKEND_ENV`.
With ``WEBGLASS_BROWSER_BACKEND=playwright`` a ``session create`` launches a
real detached Chromium and every later invocation reattaches to it through
the file store's endpoint; unset, ``session create`` records a session
without a browser and the page/action verbs keep reporting
``backend_unavailable``. The switch exists because turning the browser on by
default is a *verb-behavior* change — ``page open`` would start contacting
the network — and that belongs to t13, which owns the page and action verbs
and flips this default. t12 owns the wiring underneath it, so t13 changes one
default rather than building a session lifecycle.

Sessions across invocations
----------------------------
The store is on disk, under ``$WEBGLASS_STATE_DIR`` /
``$XDG_STATE_HOME/webglass`` / ``~/.local/state/webglass`` (see
:mod:`webglass.adapters.session_store`), so ``session create`` in one
``webglass`` process is visible to ``session show`` in the next — the M2
cross-invocation session promise (spec claim c29). A fresh store object is
built per :func:`build_service` call because it holds no state of its own:
every fact lives in the files, which is exactly what makes two processes
agree.

The seam, precisely
--------------------
:func:`build_service` merges a caller-supplied mapping over
``_DEFAULT_SERVICE_KWARGS`` and constructs one ``WebGlassService``.
``_DEFAULT_SERVICE_KWARGS`` is deliberately a **module-level dict**, not a
function-local literal, so:

* a later task can rebind one entry at process start (e.g.
  ``_factory._DEFAULT_SERVICE_KWARGS["browser"] = PlaywrightBrowserBackend()``)
  without touching any ``_commands`` module, and
* tests can monkeypatch either the dict (``monkeypatch.setitem``) or the whole
  ``build_service``/``build_context`` function to inject fakes and drive a
  complete operation lifecycle — see ``tests/test_cli_webverbs.py``.

:func:`build_context` is the same pattern for the per-invocation
``WebContext``. ``caller``/``task`` are deliberately **fixed constants**, not
minted per invocation: ``WebGlassService`` scopes session visibility
(``session list``) and lease-conflict resolution by ``(caller, task)``, so a
CLI that minted a fresh task id on every invocation would make ``session
create`` in one call invisible to (or lease-conflicting with) ``session
show``/``page open --session-id ...`` in the next — breaking exactly the
cross-invocation session flow this milestone promises (build plan t10's
design guidance). That is now load-bearing across *processes* too: the fixed
holder string is why a second CLI invocation renews its own lease instead of
being refused by the first one's. A real multi-caller/multi-task CLI identity
(and with it, genuine per-caller session isolation) is a later decision.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from webglass.adapters.browser import BrowserBackend
from webglass.adapters.clock import Clock, IdProvider  # noqa: F401 - re-exported for callers
from webglass.adapters.session_store import (
    FileSessionStore,
    SessionLauncher,
    default_sessions_dir,
    make_playwright_launcher,
)
from webglass.cli._errors import EXIT_ENV_ERROR, EXIT_SUCCESS, EXIT_USER_ERROR, CliError
from webglass.cli._output import emit_error, emit_result
from webglass.context import WebContext
from webglass.effects import OperationKind
from webglass.operations import ApplyState, CallerContext, OperationTarget, WebOperation
from webglass.results import LifecycleState, OperationError, WebOperationResult
from webglass.service import WebGlassService

__all__ = [
    "BROWSER_BACKEND_ENV",
    "BROWSER_BACKENDS",
    "SystemClock",
    "UuidIds",
    "browser_backend_name",
    "build_browser_backend",
    "build_context",
    "build_operation",
    "build_service",
    "build_session_store",
    "render_operation_result",
    "reset_browser_backends",
]


class SystemClock:
    """Real :class:`~webglass.adapters.clock.Clock`: wall-clock time via ``time.time()``."""

    def now(self) -> float:
        return time.time()


class UuidIds:
    """Real :class:`~webglass.adapters.clock.IdProvider`: ``<kind>-<uuid4 hex>`` ids."""

    def new_id(self, kind: str) -> str:
        return f"{kind}-{uuid.uuid4().hex}"


#: Selects this process's browser backend: ``playwright`` or ``none``
#: (the default). See the module docstring — this is a staging switch t13
#: flips, not a permanent knob.
BROWSER_BACKEND_ENV = "WEBGLASS_BROWSER_BACKEND"

#: Every value :data:`BROWSER_BACKEND_ENV` accepts. An unrecognized value is a
#: structured environment error (exit 2), never a silent fallback to "none":
#: a caller who asked for a browser and got a no-op would be told their page
#: verbs are unsupported when in fact their spelling was wrong.
BROWSER_BACKENDS = ("none", "playwright")

#: Fixed caller/task/evidence-namespace identity for every CLI-issued
#: operation — see the module docstring for why these are constants.
_DEFAULT_CALLER = "cli"
_DEFAULT_TASK = "cli"
_DEFAULT_EVIDENCE_NAMESPACE = "cli"
_DEFAULT_POLICY_PROFILE_REF = "built-in-default"

#: The default adapter set :func:`build_service` merges caller overrides
#: over. Deliberately module-level (not a function-local literal) — see the
#: module docstring's "the seam, precisely" section.
#: ``sessions`` and ``browser`` are present but ``None``: they are built per
#: invocation by :func:`build_session_store` / :func:`build_browser_backend`
#: (both read the environment, which a module-level literal frozen at import
#: time could not). Rebinding either entry still overrides that construction
#: — the seam is unchanged, only its default is computed rather than fixed.
_DEFAULT_SERVICE_KWARGS: dict[str, Any] = {
    "clock": SystemClock(),
    "ids": UuidIds(),
    "sessions": None,
    "browser": None,
}

#: The default ``WebContext`` field set :func:`build_context` merges caller
#: overrides over. ``workspace`` is intentionally absent here — it is
#: computed fresh on every call (the caller's cwd can change between
#: invocations) rather than frozen at import time.
_DEFAULT_CONTEXT_KWARGS: dict[str, Any] = {
    "caller": _DEFAULT_CALLER,
    "task": _DEFAULT_TASK,
    "policy_profile_ref": _DEFAULT_POLICY_PROFILE_REF,
    "evidence_namespace": _DEFAULT_EVIDENCE_NAMESPACE,
}


def browser_backend_name(environ: Mapping[str, str] | None = None) -> str:
    """Which browser backend this process is configured for.

    :raises CliError: (exit 2, environment error) on an unrecognized value.
    """
    env = os.environ if environ is None else environ
    name = env.get(BROWSER_BACKEND_ENV, "").strip().lower() or "none"
    if name not in BROWSER_BACKENDS:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"{BROWSER_BACKEND_ENV}={name!r} is not a known browser backend",
            remediation=f"set it to one of: {', '.join(BROWSER_BACKENDS)} (or leave it unset)",
        )
    return name


def build_session_store(environ: Mapping[str, str] | None = None) -> FileSessionStore:
    """The on-disk session store this invocation reads and writes.

    Given a browser backend, the store also gets its *launcher*, so ``session
    create`` starts the detached browser whose endpoint later invocations
    reattach to. With no browser backend it gets none, and ``session create``
    records a session without starting anything — the posture the default
    test suite and every browser-less caller run in.
    """
    launcher: SessionLauncher | None = None
    if browser_backend_name(environ) == "playwright":
        launcher = make_playwright_launcher()
    return FileSessionStore(default_sessions_dir(environ), launcher=launcher)


#: One browser backend per process per store directory — see
#: :func:`build_browser_backend` on why this cache is a correctness
#: requirement rather than an optimization.
_BACKEND_CACHE: dict[Path, BrowserBackend] = {}


def reset_browser_backends() -> None:
    """Drop the cached backends (test helper / long-lived-process reset).

    Does **not** disconnect them: dropping a reference is not the same as
    tearing down a live CDP connection, and a caller that wants a fresh
    backend for a different store must not thereby close the previous one's
    pages.
    """
    _BACKEND_CACHE.clear()


def build_browser_backend(
    store: FileSessionStore, environ: Mapping[str, str] | None = None
) -> BrowserBackend | None:
    """The browser backend for this process, or ``None`` if not configured.

    The backend resolves each session id to a connect endpoint through
    ``store.endpoint_for`` — the callable seam
    :class:`~webglass.adapters.playwright.PlaywrightBrowserBackend` documents
    for exactly this. That single line is what makes a browser session
    reattachable from a process that never launched it: the endpoint comes out
    of the file store, not out of this process's memory.

    **Per process, not per operation.** Playwright's sync API refuses to start
    a second driver inside a thread that already has one running ("It looks
    like you are using Playwright Sync API inside the asyncio loop"), so a
    process performing two web operations must reuse one backend — a fresh
    instance per :func:`build_service` call would make the *second* operation
    fail with a bare backend error. The cache is keyed by the store's
    directory, since that is what determines which endpoints resolve; the
    backend's own connections stay lazy, so an unused backend costs nothing.

    Playwright is imported lazily so the browser-less posture never pays for
    (or requires) the import.
    """
    if browser_backend_name(environ) != "playwright":
        return None
    cached = _BACKEND_CACHE.get(store.directory)
    if cached is not None:
        return cached
    from webglass.adapters.playwright import PlaywrightBrowserBackend

    _quiet_asyncio_teardown_chatter()
    backend = PlaywrightBrowserBackend(store.endpoint_for)
    _BACKEND_CACHE[store.directory] = backend
    return backend


def _quiet_asyncio_teardown_chatter() -> None:
    """Keep Playwright's interpreter-exit noise off stderr.

    Playwright's sync API logs ``Task was destroyed but it is pending!`` and a
    ``TargetClosedError`` traceback through the ``asyncio`` logger when the
    interpreter tears down (reproducible with no WebGlass code at all — see
    :mod:`webglass.adapters.playwright`'s module docstring). The CLI's error
    contract is that *no Python traceback ever reaches stderr*, so the layer
    that wires the browser in is the layer that has to silence it. Scoped to
    the ``asyncio`` logger and applied only when a browser is actually wired,
    so nothing else about this process's logging changes.
    """
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)


def build_service(overrides: Mapping[str, Any] | None = None) -> WebGlassService:
    """Construct the ``WebGlassService`` one CLI invocation executes operations through.

    Every ``_commands`` handler calls this (never ``WebGlassService(...)``
    directly) so the adapter set lives in exactly one place. ``overrides``
    replaces individual constructor kwargs (``search=``, ``browser=``,
    ``artifacts=``, ``policy=``, ``clock=``, ``ids=``, ``sessions=``, ...)
    without the caller needing to know the rest of the default set — and an
    override wins over the environment-driven defaults below.
    """
    kwargs: dict[str, Any] = dict(_DEFAULT_SERVICE_KWARGS)
    if overrides:
        kwargs.update(overrides)
    store = kwargs.get("sessions")
    if store is None:
        store = build_session_store()
        kwargs["sessions"] = store
    if kwargs.get("browser") is None and isinstance(store, FileSessionStore):
        kwargs["browser"] = build_browser_backend(store)
    return WebGlassService(**kwargs)


def build_context(overrides: Mapping[str, Any] | None = None) -> WebContext:
    """Construct the ``WebContext`` one CLI invocation runs its operation in."""
    kwargs: dict[str, Any] = dict(_DEFAULT_CONTEXT_KWARGS)
    kwargs["workspace"] = str(Path.cwd())
    if overrides:
        kwargs.update(overrides)
    return WebContext(**kwargs)


def build_operation(
    service: WebGlassService,
    context: WebContext,
    kind: OperationKind,
    *,
    normalized_args: Mapping[str, Any] | None = None,
    target: OperationTarget | None = None,
    session_id: str | None = None,
    apply_state: ApplyState = ApplyState.PREVIEW,
) -> WebOperation:
    """Build one ``WebOperation`` from parsed CLI args and the current context.

    The one place every noun handler goes to translate argv into the shared
    operation model — never a per-handler ad hoc dict. The operation id is
    minted from the *service's own* injected ``IdProvider`` (the same one
    ``service.execute`` itself would use for any internal id), so a
    CLI-issued operation id and a library-issued one are indistinguishable.
    """
    return WebOperation(
        operation_id=service.ids.new_id("operation"),
        kind=kind,
        normalized_args=dict(normalized_args or {}),
        caller=CallerContext(
            caller_id=context.caller,
            task_id=context.task,
            workspace_id=context.workspace,
            policy_profile_ref=(
                str(context.policy_profile_ref) if context.policy_profile_ref is not None else None
            ),
        ),
        session_id=session_id,
        target=target if target is not None else OperationTarget(),
        apply_state=apply_state,
    )


#: Lifecycle states rendered as CLI success (exit 0). ``PREVIEWED`` counts:
#: the operation did exactly what a preview is supposed to do (build plan
#: t10's exit-code design guidance — "Preview is SUCCESS exit 0, it did what
#: was asked").
_EXIT_SUCCESS_STATES = frozenset({LifecycleState.SUCCEEDED, LifecycleState.PREVIEWED})


def render_operation_result(result: WebOperationResult, *, json_mode: bool) -> int:
    """Render one ``WebOperationResult`` through the CLI output/exit contract.

    - **JSON mode** always writes ``result.to_dict()`` to stdout, success or
      not — the error, when present, is *part of* that structured payload, so
      nothing goes to stderr. This is deliberately different from a plain
      ``CliError``: a non-success ``WebOperationResult`` is a normal, fully
      structured operation outcome (policy denial, missing backend, timeout,
      ...), not a CLI usage error.
    - **Text mode** renders a compact human summary to stdout on success/
      preview, or the ``error:``/``hint:`` shape to stderr on any other
      lifecycle state — preserving the "results stdout, errors stderr, never
      mixed" contract.
    - **Exit code**: 0 for ``succeeded``/``previewed``; 1 for
      ``denied``/``blocked``/``failed``/``timed_out``/``cancelled``.
    """
    ok = result.lifecycle_state in _EXIT_SUCCESS_STATES
    if json_mode:
        emit_result(result.to_dict(), json_mode=True)
    elif ok:
        emit_result(_render_text(result), json_mode=False)
    else:
        error = result.error or OperationError(
            code="unknown_error",
            message=(
                f"operation ended in state {result.lifecycle_state.value!r} without a "
                "structured error (this is a WebGlassService bug)"
            ),
            remediation=f"file a bug with operation_id={result.operation_id}",
        )
        emit_error(
            CliError(code=EXIT_USER_ERROR, message=error.message, remediation=error.remediation),
            json_mode=False,
        )
    return EXIT_SUCCESS if ok else EXIT_USER_ERROR


def _render_text(result: WebOperationResult) -> str:
    """Compact human summary derived only from the structured result.

    Trusted control metadata (lifecycle, backend, policy decision, warnings)
    comes first; page- or provider-authored content is printed last, under an
    explicit "content (untrusted)" header — never presented as if it were a
    WebGlass diagnostic (CLAUDE.md "Target architecture" section 7).
    """
    kind = result.kind.value if isinstance(result.kind, OperationKind) else str(result.kind)
    lines = [f"{kind}: {result.lifecycle_state.value}", f"operation_id: {result.operation_id}"]
    if result.backend:
        lines.append(f"backend: {result.backend}")
    if result.policy_verdict.decision is not None:
        lines.append(f"policy: {result.policy_verdict.decision}")

    if result.lifecycle_state is LifecycleState.PREVIEWED:
        preview = result.content.trusted.get("preview")
        if isinstance(preview, dict):
            lines.append("")
            lines.append("preview (not applied — pass --apply once prepare/commit/verify lands):")
            lines.append(_indented_json(preview))

    if result.warnings:
        lines.append("")
        lines.append("warnings (webglass):")
        for warning in result.warnings:
            lines.append(f"  - {warning}")

    if result.content.untrusted:
        lines.append("")
        lines.append("content (untrusted):")
        lines.append(_indented_json(result.content.untrusted))

    return "\n".join(lines)


def _indented_json(payload: Any) -> str:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    return "\n".join(f"  {line}" for line in rendered.splitlines())
