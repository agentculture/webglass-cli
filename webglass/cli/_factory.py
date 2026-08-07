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

M1 default posture
-------------------
:func:`build_service` wires a real :class:`Clock`/:class:`IdProvider` (this
module's :class:`SystemClock`/:class:`UuidIds`) and a process-local
``InMemorySessionStore``, and deliberately injects **no** search/browser/
artifact backend. That is not an oversight:
:class:`~webglass.service.WebGlassService` already turns a missing adapter
into a structured ``backend_unavailable`` result rather than an
``AttributeError`` or a silent substitution, so ``webglass search``/``page``/
``action`` verbs round-trip through the full CLI/JSON/exit-code contract
today — they just correctly report that the backend capability does not
exist at this milestone. Real backends land at t13 (browser observation
verbs) and t15 (search provider); this module is the seam they wire through.

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
create`` in one process-local call invisible to (or lease-conflicting with)
``session show``/``page open --session-id ...`` in the next — breaking
exactly the within-process, cross-invocation session flow this milestone
promises (build plan t10's design guidance). A real multi-caller/multi-task
CLI identity is deferred to t12's on-disk ``FileSessionStore``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from webglass.adapters.clock import Clock, IdProvider  # noqa: F401 - re-exported for callers
from webglass.cli._errors import EXIT_SUCCESS, EXIT_USER_ERROR, CliError
from webglass.cli._output import emit_error, emit_result
from webglass.context import WebContext
from webglass.effects import OperationKind
from webglass.operations import ApplyState, CallerContext, OperationTarget, WebOperation
from webglass.results import LifecycleState, OperationError, WebOperationResult
from webglass.service import WebGlassService
from webglass.sessions import InMemorySessionStore

__all__ = [
    "SystemClock",
    "UuidIds",
    "build_context",
    "build_operation",
    "build_service",
    "render_operation_result",
]


class SystemClock:
    """Real :class:`~webglass.adapters.clock.Clock`: wall-clock time via ``time.time()``."""

    def now(self) -> float:
        return time.time()


class UuidIds:
    """Real :class:`~webglass.adapters.clock.IdProvider`: ``<kind>-<uuid4 hex>`` ids."""

    def new_id(self, kind: str) -> str:
        return f"{kind}-{uuid.uuid4().hex}"


#: Session state shared by every ``WebGlassService`` this factory builds
#: within one CLI process (module import is process-scoped) — the M1 answer
#: to "does `session create` in one CLI invocation stay visible to the next
#: `session show`/`session close`". Cross-*process* persistence is t12's
#: on-disk ``FileSessionStore``.
_SESSIONS: InMemorySessionStore = InMemorySessionStore()

#: Fixed caller/task/evidence-namespace identity for every CLI-issued
#: operation at M1 — see the module docstring for why these are constants.
_DEFAULT_CALLER = "cli"
_DEFAULT_TASK = "cli"
_DEFAULT_EVIDENCE_NAMESPACE = "cli"
_DEFAULT_POLICY_PROFILE_REF = "built-in-default"

#: The default adapter set :func:`build_service` merges caller overrides
#: over. Deliberately module-level (not a function-local literal) — see the
#: module docstring's "the seam, precisely" section.
_DEFAULT_SERVICE_KWARGS: dict[str, Any] = {
    "clock": SystemClock(),
    "ids": UuidIds(),
    "sessions": _SESSIONS,
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


def build_service(overrides: Mapping[str, Any] | None = None) -> WebGlassService:
    """Construct the ``WebGlassService`` one CLI invocation executes operations through.

    Every ``_commands`` handler calls this (never ``WebGlassService(...)``
    directly) so the adapter set lives in exactly one place. ``overrides``
    replaces individual constructor kwargs (``search=``, ``browser=``,
    ``artifacts=``, ``policy=``, ``clock=``, ``ids=``, ``sessions=``, ...)
    without the caller needing to know the rest of the default set.
    """
    kwargs: dict[str, Any] = dict(_DEFAULT_SERVICE_KWARGS)
    if overrides:
        kwargs.update(overrides)
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
